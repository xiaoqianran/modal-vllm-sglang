"""Cloud handoff, concurrent registration and recovery without Modal/GPU access."""
from concurrent.futures import ThreadPoolExecutor
import base64
import hashlib
import io
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from orchestration import runner, sdk, state
from shared.config.storage import VOLUME_NAME
from shared.tasks.artifacts import image_reference, resolve_frames
from shared.tasks.task_spec import prepare
from shared.utils.files import read_json

PNG = b"\x89PNG\r\n\x1a\n" + b"image-data" * 12
MP4 = b"\x00\x00\x00\x1cftypisom" + b"video-data" * 12


class FakeBackend(sdk.Backend):
    def __init__(self):
        self.submissions = []
        self.files = {}
        self.videos_complete = False
        self.download_release = threading.Event()
        self.download_release.set()
        self.download_entered = threading.Event()
        self.fail_download = False
        self.volume = Mock()
        self.volume.read_file.side_effect = lambda path: [self.files[path]]

    def fingerprint(self, spec):
        return "cache"

    def submit(self, spec, item, prefix, fingerprint):
        self.submissions.append((spec["kind"], item, prefix))
        return f"fc-{len(self.submissions)}"

    def poll(self, record):
        kind, item, prefix = self.submissions[int(record["call_id"].split("-")[1]) - 1]
        if kind == "video" and not self.videos_complete:
            return None
        data, suffix = (PNG, ".png") if kind == "image" else (MP4, ".mp4")
        filename = item["id"] + suffix
        self.files["/" + prefix + "/" + filename] = data
        return {"id": item["id"], "file": filename, "bytes": len(data), "status": "completed",
                "sha256": hashlib.sha256(data).hexdigest(), "intent_hash": record["intent_hash"],
                "execution_hash": record["execution_hash"]}

    def download(self, *args):
        self.download_entered.set()
        if not self.download_release.wait(5):
            raise TimeoutError("Test did not release download")
        if self.fail_download:
            raise OSError("Disk unavailable")
        return super().download(*args)


class SDKWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(runner.ROOT / "workflows", self.root / "workflows")
        (self.root / "tasks").mkdir()
        self.backend = FakeBackend()
        self.addCleanup(self.backend.download_release.set)

    def create(self, name="006-cloud-demo", count=1):
        path = self.root / "tasks" / (name + ".json")
        task = {"version": 1, "name": name, "workflow": "storyboard-to-video",
                "items": [{"id": f"{i:03}", "first_prompt": "boat near shore", "last_prompt": "boat at sea",
                           "motion_prompt": "boat drifting"} for i in range(1, count + 1)]}
        state.save(path, task)
        plan = runner.build_plan(path, root=self.root)
        return runner.new_run(plan, root=self.root)

    def test_sdk_plan_is_offline_and_needs_no_http_configuration(self):
        with patch.dict("os.environ", {}, clear=True):
            directory = self.create()
        plan = read_json(directory / "plan.json")
        self.assertEqual(plan["transport"], "sdk")
        self.assertEqual(plan["volume"], "modal-vllm-sglang")
        self.assertFalse((self.root / "endpoints.example.json").exists())

    def test_run_cli_registers_mixed_workflows_and_returns_without_cloud_access(self):
        self.create()
        single = self.root / "tasks/006-single-demo.json"
        state.save(single, {"version": 1, "name": "006-single-demo", "workflow": "moss-t2i",
                            "items": [{"id": "001", "prompt": "boat"}]})
        original_plan, original_run = runner.build_plan, runner.new_run
        paths = [str(self.root / "tasks/006-cloud-demo.json"), str(single)]
        with patch("sys.argv", ["orchestration", "run", *paths]), \
                patch.object(runner, "build_plan", side_effect=lambda path: original_plan(path, root=self.root)), \
                patch.object(runner, "new_run", side_effect=lambda plan, output: original_run(plan, root=self.root, output=output)), \
                patch("orchestration.sdk.Backend") as backend, \
                patch("sys.stdout", new=io.StringIO()) as output:
            runner.main()
        backend.assert_not_called()
        self.assertIn("Registration complete", output.getvalue())
        for name, count in (("006-cloud-demo", 3), ("006-single-demo", 1)):
            current = read_json(self.root / "outputs" / name / "001-main/state.json")
            self.assertEqual(len(current["jobs"]), count)
            self.assertTrue(all(record["status"] == "pending" for record in current["jobs"].values()))
            self.assertEqual(current["collection_epoch"], 1)

    def test_removed_submission_flags_are_not_accepted(self):
        for args in (("--enqueue",), ("--transport", "http"), ("--timeout", "60")):
            with self.subTest(args=args), patch("sys.argv", ["orchestration", "run", "task.json", *args]), \
                    patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit) as exit:
                runner.main()
            self.assertEqual(exit.exception.code, 2)

    def test_resume_retries_downloads_in_existing_worker_without_resubmission(self):
        directory = self.create()
        self.backend.videos_complete = True
        self.backend.fail_download = True
        downloads = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            for _ in range(3):
                sdk.tick(directory, self.backend, downloads, pool)
            for entry in downloads.values():
                with self.assertRaises(OSError):
                    entry["future"].result(timeout=2)
            result = sdk.tick(directory, self.backend, downloads, pool)
            self.assertEqual(result["download_errors"], 3)
            self.backend.fail_download = False
            sdk.request_resume(directory)
            sdk.tick(directory, self.backend, downloads, pool)
            for entry in downloads.values():
                entry["future"].result(timeout=2)
            result = sdk.tick(directory, self.backend, downloads, pool)
        self.assertEqual(result["download_errors"], 0)
        self.assertEqual(result["local_downloaded"], 3)
        self.assertEqual(len(self.backend.submissions), 3)

    def test_video_uses_cloud_images_while_downloads_are_blocked(self):
        directory = self.create(count=2)
        self.backend.download_release.clear()
        downloads = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            try:
                sdk.tick(directory, self.backend, downloads, pool)
                self.assertEqual([item["id"] for kind, item, _ in self.backend.submissions],
                                 ["j" + sdk.intent_digest(key)[:32] for key in
                                  ("first/001", "last/001", "first/002", "last/002")])
                sdk.tick(directory, self.backend, downloads, pool)
                self.assertTrue(self.backend.download_entered.wait(2))
                videos = [item for kind, item, _ in self.backend.submissions if kind == "video"]
                self.assertEqual(len(videos), 2)
                self.assertFalse(list(directory.glob("*.png")))
                for item in videos:
                    self.assertIn("first_frame_artifact", item)
                    self.assertIn("last_frame_artifact", item)
                    self.assertNotIn("first_frame_base64", item)
                    self.assertTrue(item["first_frame_artifact"]["path"].startswith("model-results/moss/"))
                # Pika reads committed Volume bytes, independently of host downloads.
                spec = read_json(runner.ROOT / "002-pika-h3-flex/model.json")
                payload = resolve_frames(prepare(spec, videos[0])[0], self.backend.volume)
                self.assertEqual(base64.b64decode(payload["first_frame_base64"]), PNG)
                self.assertNotIn("first_frame_artifact", payload)
            finally:
                self.backend.download_release.set()

    def test_new_task_and_scene_append_progress_during_slow_video(self):
        first = self.create()
        downloads = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            sdk.tick(first, self.backend, downloads, pool)
            sdk.tick(first, self.backend, downloads, pool)
            self.assertEqual(sum(kind == "video" for kind, _, _ in self.backend.submissions), 1)
            second = self.create("006-second-demo")
            sdk.tick(second, self.backend, downloads, pool)
            self.assertEqual(sum(kind == "image" for kind, _, _ in self.backend.submissions), 4)
            # Append while the first scene's video is still queued/running.
            self.assertEqual(self.create(count=2), first)
            sdk.tick(first, self.backend, downloads, pool)
            self.assertEqual(sum(kind == "image" for kind, _, _ in self.backend.submissions), 6)
            self.assertEqual(sdk.snapshot(first)[1]["jobs"]["video/001"]["status"], "queued")

    def test_slow_spawn_does_not_hold_root_or_scene_lock(self):
        first = self.create()
        entered, release = threading.Event(), threading.Event()
        errors = []
        original = self.backend.submit

        def slow_submit(spec, item, prefix, fingerprint):
            if "006-cloud-demo" in prefix:
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("Test did not release spawn")
            return original(spec, item, prefix, fingerprint)

        self.backend.submit = slow_submit
        downloads = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            def tick_first():
                try:
                    sdk.tick(first, self.backend, downloads, pool)
                except BaseException as exc:
                    errors.append(exc)
            thread = threading.Thread(target=tick_first)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                second = self.create("006-independent-demo")
                sdk.tick(second, self.backend, {}, pool)
                self.create(count=2)
                self.assertEqual(len(self.backend.submissions), 2)
            finally:
                release.set()
                thread.join(6)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_download_failure_does_not_block_video_and_resume_never_resubmits(self):
        directory = self.create()
        self.backend.fail_download = True
        self.backend.videos_complete = True
        result = sdk.execute(directory, backend=self.backend, timeout=3, poll=0.001)
        self.assertEqual(result["counts"], {"completed": 3})
        self.assertEqual(result["download_errors"], 3)
        self.assertEqual(len(self.backend.submissions), 3)
        self.backend.fail_download = False
        result = sdk.execute(directory, backend=self.backend, timeout=3, poll=0.001)
        self.assertEqual(result["download_errors"], 0)
        self.assertEqual(result["local_downloaded"], 3)
        self.assertEqual(len(self.backend.submissions), 3)

    def test_uncertain_spawn_is_not_resubmitted(self):
        directory = self.create()
        with ThreadPoolExecutor(max_workers=8) as pool:
            with patch.object(self.backend, "submit", side_effect=OSError("Lost reply")):
                with self.assertRaises(OSError):
                    sdk.tick(directory, self.backend, {}, pool)
            self.backend.poll = lambda record: None
            sdk.tick(directory, self.backend, {}, pool)
        records = sdk.snapshot(directory)[1]["jobs"]
        self.assertEqual(records["first/001"]["status"], "uncertain")
        self.assertEqual(records["last/001"]["status"], "queued")
        self.assertEqual(len(self.backend.submissions), 1)

    def test_resume_restores_deleted_local_file_and_refuses_corruption(self):
        directory = self.create()
        self.backend.videos_complete = True
        sdk.execute(directory, backend=self.backend, timeout=3, poll=0.001)
        target = directory / "001-first.png"
        target.unlink()
        result = sdk.execute(directory, backend=self.backend, timeout=3, poll=0.001)
        self.assertEqual(target.read_bytes(), PNG)
        self.assertEqual(result["download_errors"], 0)
        target.write_bytes(b"unrelated content")
        result = sdk.execute(directory, backend=self.backend, timeout=3, poll=0.001)
        self.assertEqual(result["download_errors"], 1)
        self.assertEqual(target.read_bytes(), b"unrelated content")
        self.assertEqual(len(self.backend.submissions), 3)

    def test_batch_advances_other_scene_while_first_video_is_pending(self):
        directories = [self.create(), self.create("006-batch-demo")]
        result = sdk.execute_many(directories, backend=self.backend, timeout=0.15, poll=0.001)
        self.assertEqual(len(self.backend.submissions), 6)
        self.assertTrue(all(r["counts"] == {"completed": 2, "queued": 1} for r in result))

    def test_frozen_plan_and_storage_identity_cannot_be_changed(self):
        directory = self.create()
        plan = read_json(directory / "plan.json")
        plan["volume"] = "old-storage"
        state.save(directory / "plan.json", plan)
        with self.assertRaises(ValueError):
            sdk.snapshot(directory)


class CloudImageTests(unittest.TestCase):
    def ref(self, path="model-results/moss/image.png"):
        return {"path": path, "sha256": hashlib.sha256(PNG).hexdigest(), "size": len(PNG)}

    def test_rejects_traversal_wrong_hash_and_oversized_reads(self):
        for path in ("/etc/passwd", "model-results/../secret", "model-results\\image.png", "weights/model.bin"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                image_reference(self.ref(path))
        volume = Mock()
        for data in (PNG + b"extra", PNG[:-1], PNG[:-1] + b"z"):
            volume.read_file.return_value = [data]
            with self.assertRaises(ValueError):
                resolve_frames({"first_frame_artifact": self.ref()}, volume)

    def test_inline_and_cloud_inputs_are_mutually_exclusive(self):
        spec = read_json(runner.ROOT / "002-pika-h3-flex/model.json")
        item = {"id": "001", "operation": "fl2va", "prompt": "boat", "seed": 1,
                "first_frame_artifact": self.ref(), "last_frame_artifact": self.ref(),
                "first_frame_base64": base64.b64encode(PNG).decode()}
        with self.assertRaises(ValueError):
            prepare(spec, item)

    def test_shared_volume_identity(self):
        from shared.serving.modal_setup import model_volume
        self.assertEqual(VOLUME_NAME, "modal-vllm-sglang")
        with patch("modal.Volume.from_name") as factory:
            model_volume()
        factory.assert_called_once_with(VOLUME_NAME, create_if_missing=True)

    def test_cloud_worker_resolves_frames_and_commits_result_before_return(self):
        from shared.serving.model_service import execute
        spec = read_json(runner.ROOT / "002-pika-h3-flex/model.json")
        item = {"id": "001", "operation": "fl2va", "prompt": "boat", "seed": 1,
                "first_frame_artifact": self.ref(), "last_frame_artifact": self.ref()}
        signature = prepare(spec, item)[1]
        volume = Mock()
        volume.read_file.return_value = [PNG]
        with tempfile.TemporaryDirectory() as temporary, \
                patch("shared.serving.model_service.Path", wraps=Path) as paths, \
                patch("shared.serving.inference_api.SGLangVideoClient") as factory:
            paths.side_effect = lambda value: Path(temporary) if value == "/mnt/modal" else Path(value)
            client = factory.return_value
            client.submit.return_value = "engine-job-1"
            client.content.return_value = MP4
            result = next(execute(spec, [item], "model-results/test", volume, "cache"))
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["intent_hash"], signature)
            self.assertEqual((Path(temporary) / "model-results/test/001.mp4").read_bytes(), MP4)
            payload = client.submit.call_args.args[0]
            self.assertEqual(base64.b64decode(payload["first_frame_base64"]), PNG)
            self.assertNotIn("first_frame_artifact", payload)
            self.assertGreaterEqual(volume.commit.call_count, 3)
            volume.reload.assert_not_called()

    def test_backend_recovers_committed_result_without_call_id(self):
        import json
        backend = sdk.Backend.__new__(sdk.Backend)
        backend.volume, backend.modal = Mock(), Mock()
        record = {"prefix": "model-results/moss/test", "id": "001", "intent_hash": "intent", "execution_hash": "execution"}
        result = {"id": "001", "file": "001.png", "status": "completed", "bytes": len(PNG),
                  "sha256": hashlib.sha256(PNG).hexdigest(), "intent_hash": "intent", "execution_hash": "execution"}
        backend.volume.read_file.return_value = [json.dumps(result).encode()]
        self.assertEqual(backend.poll(record), result)
        backend.modal.FunctionCall.from_id.assert_not_called()
