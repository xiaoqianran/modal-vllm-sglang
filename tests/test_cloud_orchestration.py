"""Cloud handoff, durable ownership and independent telemetry recovery."""
from concurrent.futures import ThreadPoolExecutor
import copy
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from pipeline import s03_run_shot as cloud, s02_submit_shots as cloud_client
from pipeline import s01_build_plan as runner
from orchestration import protocol, sdk, state
from runtime.tasks.task_identity import intent_digest
from runtime.telemetry.collector import Collector, collect_cloud
from runtime.telemetry.recorder import Recorder, close_local
from runtime.telemetry.storage import Writer
from runtime.utils.files import read_json
from tests.test_sdk_orchestration import FakeBackend


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "outputs"
        self.remote = self.root / "remote"
        self.addCleanup(close_local, self.root / ".runtime/logs")
        shutil.copytree(runner.ROOT / "workflows", self.root / "workflows")
        self.task = self.root / "tasks/006-cloud.json"
        self.create()

    def create(self, count=1):
        state.save(self.task, {"version": 1, "name": "006-cloud", "workflow": "storyboard-to-video",
            "items": [{"id": f"{i:03}", "first_prompt": "shore", "last_prompt": "lake",
                       "motion_prompt": "drift"} for i in range(1, count + 1)]})
        self.directory = runner.new_run(runner.build_plan(self.task, root=self.root), root=self.root)
        return self.directory

    def local_backend(self, model):
        class Local(cloud_client.Backend):
            def __init__(inner):
                inner.volume = model.volume
                inner.accepted = []
            def persist_workflow(inner, payload, identity):
                inner.persisted = (payload, identity)
            def submit_workflow(inner, payload, identity, *, persisted=False):
                assert persisted and inner.persisted == (payload, identity)
                inner.accepted.append((payload, identity))
                return "fc-workflow"
            def poll_workflow(inner, receipt):
                path = self.remote / protocol.state_path(receipt["id"])
                return read_json(path) if path.exists() else None
        return Local()

    def test_explicit_recovery_consumes_request_and_tracks_new_controller(self):
        backend = self.local_backend(FakeBackend())
        backend.modal = Mock()
        with ThreadPoolExecutor(max_workers=2) as pool:
            cloud_client.tick_shot(self.directory, "001", backend, {}, pool)
            cloud_client.save_receipt(self.directory, "001", recovery_requested=True, status="uncertain",
                                      controller_state="terminal", deployment_version="a" * 64)
            with patch("orchestration.workflow_recovery.load_plan", return_value={}), patch(
                "tools.recover_workflow.resume", return_value={"call_id": "fc-recovered"}
            ) as resume:
                cloud_client.tick_shot(self.directory, "001", backend, {}, pool)
                cloud_client.tick_shot(self.directory, "001", backend, {}, pool)
                resume.assert_called_once_with(backend.volume, {}, backend.modal, "a" * 64)
        receipt = sdk.snapshot(self.directory)[1]["cloud_runs"]["001"]
        self.assertEqual(receipt["call_id"], "fc-recovered")
        self.assertFalse(receipt["recovery_requested"])
        self.assertEqual(len(backend.accepted), 1)

    def test_cloud_finishes_video_with_no_local_worker_then_downloads_on_restart(self):
        model = FakeBackend()
        model.videos_complete = True
        local = self.local_backend(model)
        with ThreadPoolExecutor(max_workers=2) as pool:
            cloud_client.tick_shot(self.directory, "001", local, {}, pool)
        self.assertEqual(len(local.accepted), 1)
        # No local tick runs while both models execute and the video is handed off.
        payload, identity = local.accepted[0]
        result = cloud.run(payload, identity, backend=model, root=self.remote, poll=0.001)
        self.assertEqual(result["status"], "completed")
        self.assertEqual([kind for kind, _, _ in model.submissions], ["image", "image", "video"])
        video = model.submissions[-1][1]
        self.assertIn("first_frame_artifact", video)
        self.assertIn("last_frame_artifact", video)
        self.assertFalse(list(self.directory.glob("*.png")))
        downloads = {}
        with patch("pipeline.s02_submit_shots.enqueue"), ThreadPoolExecutor(max_workers=2) as pool:
            cloud_client.tick_shot(self.directory, "001", local, downloads, pool)
            for entry in downloads.values():
                entry["future"].result(timeout=2)
            report = cloud_client.tick_shot(self.directory, "001", local, downloads, pool)
        self.assertEqual(report["local_downloaded"], 3)
        self.assertEqual(len(local.accepted), 1)

    def test_lost_workflow_receipt_never_resubmits_and_recovers_by_volume_identity(self):
        model = FakeBackend()
        model.videos_complete = True
        local = self.local_backend(model)
        submit = local.submit_workflow
        def lost(*args, **kwargs):
            submit(*args, **kwargs)
            raise OSError("reply lost")
        local.submit_workflow = lost
        with ThreadPoolExecutor(max_workers=2) as pool:
            with self.assertRaises(OSError):
                cloud_client.tick_shot(self.directory, "001", local, {}, pool)
            cloud_client.tick_shot(self.directory, "001", local, {}, pool)
        self.assertEqual(len(local.accepted), 1)
        payload, identity = local.accepted[0]
        cloud.run(payload, identity, backend=model, root=self.remote, poll=0.001)
        with patch("pipeline.s02_submit_shots.enqueue"), ThreadPoolExecutor(max_workers=2) as pool:
            cloud_client.tick_shot(self.directory, "001", local, {}, pool)
        self.assertEqual(sdk.snapshot(self.directory)[1]["cloud_runs"]["001"]["status"], "completed")

    def test_spawn_intent_is_committed_before_model_acceptance(self):
        backend = FakeBackend()
        backend.videos_complete = True
        plan = sdk.snapshot(self.directory)[0]
        payload = cloud_client.payload_for(self.directory, plan, "001", backend)
        identity = protocol.run_id(plan, "001")
        submit = backend.submit
        def checked(spec, item, prefix, fingerprint):
            current = read_json(self.remote / protocol.state_path(identity))
            self.assertEqual(current["jobs"][item["_telemetry"]["job_key"]]["status"], "submitting")
            return submit(spec, item, prefix, fingerprint)
        backend.submit = checked
        cloud.run(payload, identity, backend=backend, root=self.remote, poll=0.001)

    def test_durable_single_pass_resumes_dependencies_without_poll_sleep(self):
        backend = FakeBackend()
        backend.videos_complete = True
        plan = sdk.snapshot(self.directory)[0]
        payload = cloud_client.payload_for(self.directory, plan, "001", backend)
        identity = protocol.run_id(plan, "001")
        previous = None
        with patch.object(cloud.time, "sleep", side_effect=AssertionError("advance waited")):
            for _ in range(5):
                previous = cloud.run(payload, identity, backend=backend, root=self.remote,
                                     durable=True, previous=previous, single_pass=True)
                self.assertEqual(previous, read_json(self.remote / protocol.state_path(identity)))
                if previous["status"] == "completed":
                    break
        self.assertEqual(previous["status"], "completed")
        self.assertEqual(len(backend.submissions), 3)

    def test_round_coalesces_checkpoints_but_preserves_every_spawn_barrier(self):
        backend = FakeBackend()
        backend.videos_complete = True
        plan = sdk.snapshot(self.directory)[0]
        payload = cloud_client.payload_for(self.directory, plan, "001", backend)
        identity = protocol.run_id(plan, "001")
        destination = self.remote / protocol.state_path(identity)
        committed = []
        backend.volume.commit.side_effect = lambda: committed.append(copy.deepcopy(read_json(destination)))
        submit = backend.submit
        def checked(spec, item, prefix, fingerprint):
            key = item["_telemetry"]["job_key"]
            self.assertEqual(committed[-1]["jobs"][key]["status"], "submitting")
            if spec["kind"] == "video":
                self.assertEqual(committed[-1]["jobs"]["first/001"]["status"], "completed")
                self.assertEqual(committed[-1]["jobs"]["last/001"]["status"], "completed")
            return submit(spec, item, prefix, fingerprint)
        backend.submit = checked
        backend.model_readiness = Mock(return_value={"status": "ready"})
        result = cloud.run(payload, identity, backend=backend, root=self.remote, poll=0.001)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(backend.submissions), 3)
        # Initial, 3 per-job intent barriers, 2 round-end snapshots,
        # and final completed (not a separate running/completed pair).
        self.assertEqual(len(committed), 7)
        self.assertEqual(backend.model_readiness.call_count, 3)
        self.assertEqual(committed[-1], result)
        self.assertFalse(backend.defer_checkpoint)

    def test_failed_intent_commit_prevents_spawn(self):
        backend = FakeBackend()
        plan = sdk.snapshot(self.directory)[0]
        payload = cloud_client.payload_for(self.directory, plan, "001", backend)
        identity = protocol.run_id(plan, "001")
        backend.volume.commit.side_effect = [None, OSError("intent not committed"), None]
        with self.assertRaisesRegex(OSError, "intent not committed"):
            cloud.run(payload, identity, backend=backend, root=self.remote, poll=0.001)
        self.assertFalse(backend.submissions)
        self.assertIsNone(backend.checkpoint)
        self.assertFalse(backend.defer_checkpoint)

    def test_spawn_receipt_lost_before_round_commit_remains_observable(self):
        backend = FakeBackend()
        backend.videos_complete = True
        plan = sdk.snapshot(self.directory)[0]
        payload = cloud_client.payload_for(self.directory, plan, "001", backend)
        identity = protocol.run_id(plan, "001")
        destination = self.remote / protocol.state_path(identity)
        committed = []
        backend.volume.commit.side_effect = lambda: committed.append(copy.deepcopy(read_json(destination)))
        submit = backend.submit
        def lost(*args):
            submit(*args)
            raise OSError("accepted but caller lost reply")
        backend.submit = lost
        with self.assertRaisesRegex(OSError, "accepted"):
            cloud.run(payload, identity, backend=backend, root=self.remote, poll=0.001)
        self.assertEqual(len(backend.submissions), 1)
        self.assertEqual(committed[-2]["jobs"]["first/001"]["status"], "submitting")
        self.assertEqual(committed[-1]["jobs"]["first/001"]["status"], "uncertain")
        self.assertEqual(committed[-1]["status"], "uncertain")
        backend.submit = submit
        # The local FakeBackend stands in for an authoritative completed result
        # recovered by identity after the receipt has been lost.
        poll = backend.poll
        def observe(record):
            if not record.get("call_id"):
                record = {**record, "call_id": "fc-1"}
            return poll(record)
        backend.poll = observe
        result = cloud.run(payload, identity, backend=backend, root=self.remote,
                           durable=True, previous=committed[-1], poll=0.001)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(backend.submissions), 3)

    def test_unchanged_polls_do_not_commit_again(self):
        backend = FakeBackend()
        plan = sdk.snapshot(self.directory)[0]
        payload = cloud_client.payload_for(self.directory, plan, "001", backend)
        identity = protocol.run_id(plan, "001")
        def unchanged(directory, backend, *_):
            for _ in range(5):
                backend.checkpoint(directory)
        with patch("pipeline.s03_run_shot.tick", side_effect=unchanged):
            result = cloud.run(payload, identity, backend=backend, root=self.remote, timeout=0)
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(backend.volume.commit.call_count, 2)

    def test_checkpoint_failure_closes_writer_and_releases_callback(self):
        for failed_commit in (1, 2):
            with self.subTest(failed_commit=failed_commit):
                backend = FakeBackend()
                plan = sdk.snapshot(self.directory)[0]
                payload = cloud_client.payload_for(self.directory, plan, "001", backend)
                identity = protocol.run_id(plan, "001")
                remote = self.remote / str(failed_commit)
                writer = Writer(remote / "telemetry", relative="telemetry")
                self.addCleanup(writer.close)
                commits = 0

                def commit():
                    nonlocal commits
                    commits += 1
                    if commits >= failed_commit:
                        raise OSError("commit unavailable")

                backend.volume.commit.side_effect = commit
                with patch("pipeline.s03_run_shot.cloud_recorder", return_value=Recorder(writer)), \
                        patch("pipeline.s03_run_shot.tick", side_effect=RuntimeError("scheduler failed")):
                    with self.assertRaises((OSError, RuntimeError)):
                        cloud.run(payload, identity, backend=backend, root=remote, poll=0.001)
                self.assertTrue(writer.closed)
                self.assertFalse(writer.thread.is_alive())
                self.assertIsNone(backend.checkpoint)
                self.assertEqual(backend.submissions, [])

    def test_appended_shot_can_finish_while_previous_video_is_slow(self):
        self.create(count=2)
        plan = sdk.snapshot(self.directory)[0]
        first, second = FakeBackend(), FakeBackend()
        second.videos_complete = True
        first_video = threading.Event()
        submit = first.submit
        def observed(*args):
            result = submit(*args)
            if args[0]["kind"] == "video":
                first_video.set()
            return result
        first.submit = observed
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(cloud.run, cloud_client.payload_for(self.directory, plan, "001", first),
                                 protocol.run_id(plan, "001"), backend=first, root=self.remote, poll=0.01, timeout=5)
            try:
                self.assertTrue(first_video.wait(2))
                result = cloud.run(cloud_client.payload_for(self.directory, plan, "002", second),
                                   protocol.run_id(plan, "002"), backend=second, root=self.remote, poll=0.001)
                self.assertEqual(result["status"], "completed")
                self.assertFalse(future.done())
            finally:
                first.videos_complete = True
            self.assertEqual(future.result(timeout=3)["status"], "completed")

    def test_collector_recovers_terminal_job_without_new_enqueue(self):
        writer = Writer(self.remote / "telemetry", relative="telemetry")
        self.addCleanup(writer.close)
        task_id = "006-cloud/001-main"
        with Recorder(writer, task_id=task_id, job_key="first/001").span("generate"):
            pass
        manifest = writer.publish(writer.reference())
        sdk.update(self.directory, "first/001", status="completed", result={"telemetry": manifest})
        volume = Mock()
        volume.read_file.side_effect = lambda path: [(self.remote / path.removeprefix("/inference/").lstrip("/")).read_bytes()]
        collector = Collector(self.output, volume)
        self.addCleanup(collector.close)
        path = self.output / ".telemetry/reports" / (intent_digest(task_id) + ".json")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if path.exists() and any(s["stage"] == "generate" for s in read_json(path)["stages"]):
                break
            time.sleep(0.02)
        self.assertTrue(any(s["stage"] == "generate" for s in read_json(path)["stages"]))

    def test_incomplete_final_publication_does_not_retry_forever(self):
        writer = Writer(self.remote / "telemetry", relative="telemetry")
        self.addCleanup(writer.close)
        with Recorder(writer).span("generate"):
            pass
        writer.flush()
        ref = writer.reference()
        with patch.object(writer, "flush", return_value={**writer.manifest(), "complete": False}):
            manifest = writer.publish(ref)
        self.assertFalse(manifest["complete"])
        volume = Mock()
        volume.read_file.side_effect = lambda path: [(self.remote / path.removeprefix("/inference/").lstrip("/")).read_bytes()]
        self.assertFalse(collect_cloud(self.output, volume, ref)["pending"])

    def test_background_publication_does_not_block_caller_and_manifests_are_incremental(self):
        writer = Writer(self.remote / "telemetry", relative="telemetry")
        self.addCleanup(writer.close)
        entered, release = threading.Event(), threading.Event()
        def commit():
            entered.set()
            release.wait(3)
        with Recorder(writer).span("generate"):
            pass
        first = writer.reference()
        writer.publish_async(first, commit)
        try:
            self.assertTrue(entered.wait(1))
            with Recorder(writer).span("generate"):
                pass
            second = writer.reference()
            writer.publish_async(second, lambda: None)
        finally:
            release.set()
        writer.flush()
        a = read_json(self.remote / first["manifest_path"])
        b = read_json(self.remote / second["manifest_path"])
        self.assertTrue(a["complete"] and b["complete"])
        self.assertTrue({p["path"] for p in a["paths"]}.isdisjoint(p["path"] for p in b["paths"]))

    def test_cloud_deployment_has_no_gpu_and_duplicate_claim_cannot_run(self):
        from pipeline import deploy_shot as cloud_app
        payload = {"example": "unused"}
        with patch.object(cloud_app, "volume") as volume, patch.object(cloud_app, "claims") as claims, \
                patch.object(cloud_app, "run") as run:
            volume.read_file.side_effect = FileNotFoundError
            claims.put.return_value = False
            with self.assertWarnsRegex(UserWarning, "executing locally.*mounted Volume"):
                with self.assertRaisesRegex(RuntimeError, "already claimed"):
                    cloud_app.run_shot.local(payload, "a" * 64)
            run.assert_not_called()
            self.assertTrue(claims.put.call_args.kwargs["skip_if_exists"])
