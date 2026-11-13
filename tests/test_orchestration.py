"""Behavioral tests: local temporary runs, fake services, no GPU/network."""
import copy
import base64
import json
import os
import shutil
import tempfile
import unittest
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from orchestration import adapters, runner, state
from shared.files import read_json, sha256_file

PNG = b"\x89PNG\r\n\x1a\n" + b"test-image" * 10
MP4 = b"\x00\x00\x00\x1cftypisom" + b"test-video" * 10


class FakeServices:
    def __init__(self):
        self.submissions = []
        self.polls = []
        self.downloads = []
        self.submit_error = None
        self.poll_error = None
        self.download_error = None
        self.remote_status = "completed"

    def __call__(self, uses, endpoint, headers):
        service = self

        class FakeAdapter(adapters.Adapter):
            def submit(self, payload, timeout):
                service.submissions.append((uses, payload))
                if service.submit_error:
                    raise service.submit_error
                return {"data": PNG} if self.output == "image" else {"job_id": "remote-001"}

            def poll(self, job_id, timeout):
                service.polls.append(job_id)
                if service.poll_error:
                    raise service.poll_error
                return service.remote_status

            def collect(self, job_id, timeout):
                service.downloads.append(job_id)
                if service.download_error:
                    raise service.download_error
                return MP4

        return FakeAdapter(uses, endpoint, headers)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(runner.ROOT / "workflows", self.root / "workflows")
        shutil.copytree(runner.ROOT / "tasks", self.root / "tasks")
        shutil.copy(runner.ROOT / "endpoints.example.json", self.root)
        self.env = patch.dict(os.environ, {"MOSS_ENDPOINT": "http://moss.test", "PIKA_ENDPOINT": "http://pika.test",
                                          "MOSS_MODAL_KEY": "private-key", "MOSS_MODAL_SECRET": "private-secret"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.task_path = self.root / "tasks/001-story-demo.json"
        self.services = FakeServices()

    def plan(self):
        return runner.build_plan(self.task_path, root=self.root)

    def create(self, workflow=None):
        if workflow:
            task = read_json(self.task_path)
            task["workflow"] = workflow
            task["items"] = [{"id": "001", "prompt": "A quiet ocean"}]
            state.save(self.task_path, task)
        return runner.new_run(self.plan(), root=self.root)

    def execute(self, directory):
        return runner.execute(directory, adapter_factory=self.services, timeout=1, poll=0.001)

    def test_stage_order_material_transfer_resume_and_no_secrets(self):
        task = read_json(self.task_path)
        task["items"].append(dict(task["items"][0], id="002"))
        state.save(self.task_path, task)
        directory = self.create()
        result = self.execute(directory)
        self.assertEqual(result["counts"], {"completed": 6})
        self.assertEqual([x[0] for x in self.services.submissions], ["moss.t2i"] * 4 + ["pika.fl2va"] * 2)
        for stem in ("001-first.png", "001-last.png", "001.mp4",
                     "002-first.png", "002-last.png", "002.mp4"):
            self.assertTrue((directory / stem).is_file(), stem)
        self.assertIn("first_frame_base64", self.services.submissions[-1][1])
        self.execute(directory)
        self.assertEqual(len(self.services.submissions), 6)
        for path in directory.rglob("*.json"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("private-secret", text)
            self.assertNotIn("private-key", text)
            self.assertNotIn("first_frame_base64", text)

    def test_seed_stable_on_reordering_and_defaults_frozen(self):
        task = read_json(self.task_path)
        task["items"].append(dict(task["items"][0], id="002"))
        state.save(self.task_path, task)
        before = self.plan()
        task["items"].reverse()
        state.save(self.task_path, task)
        after = self.plan()
        self.assertEqual({j["key"]: j["inputs"]["seed"] for j in before["jobs"]},
                         {j["key"]: j["inputs"]["seed"] for j in after["jobs"]})
        self.assertEqual(before["jobs"][0]["params"]["guidance_scale"], 3.0)

    def test_scene_has_no_run_id_and_new_items_append_without_recreating_old_media(self):
        task = read_json(self.task_path)
        task.update(project="002-again-love", scene="001-hotel-corridor",
                    workflow="moss-t2i", items=[{"id": "001", "prompt": "First scene"}])
        state.save(self.task_path, task)
        directory = runner.new_run(self.plan(), root=self.root)
        self.assertEqual(directory.relative_to(self.root).as_posix(),
                         "outputs/002-again-love/001-hotel-corridor")
        self.assertEqual(self.execute(directory)["counts"], {"completed": 1})
        before = sha256_file(directory / "001.png")
        self.assertEqual(runner.new_run(self.plan(), root=self.root), directory)
        task["items"].append({"id": "002", "prompt": "Second scene"})
        state.save(self.task_path, task)
        self.assertEqual(runner.new_run(self.plan(), root=self.root), directory)
        self.assertEqual(self.execute(directory)["counts"], {"completed": 2})
        self.assertEqual(sha256_file(directory / "001.png"), before)
        self.assertTrue((directory / "002.png").is_file())
        self.assertEqual(len(self.services.submissions), 2)
        task["items"][0]["prompt"] = "Attempted rewrite"
        state.save(self.task_path, task)
        with self.assertRaises(ValueError):
            runner.new_run(self.plan(), root=self.root)

    def test_existing_untracked_image_blocks_submission_without_overwrite(self):
        task = read_json(self.task_path)
        task["workflow"] = "moss-t2i"
        task["items"] = [{"id": "001", "prompt": "Do not replace previous image"}]
        state.save(self.task_path, task)
        scene = self.root / "outputs" / task["name"] / "001-main"
        scene.mkdir(parents=True)
        (scene / "001.png").write_bytes(PNG)
        directory = runner.new_run(self.plan(), root=self.root)
        self.assertEqual(self.execute(directory)["counts"], {"failed": 1})
        self.assertEqual((scene / "001.png").read_bytes(), PNG)
        self.assertEqual(self.services.submissions, [])

    def test_explicit_reuse_of_migrated_image_does_not_submit_gpu(self):
        task = read_json(self.task_path)
        task["workflow"] = "moss-t2i"
        task["items"] = [{"id": "001", "prompt": "Previous image", "reuse_existing": True}]
        state.save(self.task_path, task)
        scene = self.root / "outputs" / task["name"] / "001-main"
        scene.mkdir(parents=True)
        (scene / "001.png").write_bytes(PNG)
        directory = runner.new_run(self.plan(), root=self.root)
        self.assertEqual(self.execute(directory)["counts"], {"completed": 1})
        self.assertEqual(self.services.submissions, [])
        record = read_json(directory / "state.json")["jobs"]["image/001"]
        self.assertEqual(record["origin"], "explicit-existing-file")
        self.assertEqual(record["artifacts"]["image"]["sha256"], sha256_file(scene / "001.png"))
        self.assertEqual(self.execute(directory)["counts"], {"completed": 1})

    def test_offline_plan_and_validation_before_submission(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(len(self.plan()["jobs"]), 3)
        workflow_path = self.root / "workflows/storyboard-to-video.json"
        original = read_json(workflow_path)
        bad_graphs = []
        graph = copy.deepcopy(original)
        graph["steps"][0]["inputs"]["prompt"] = {"ref": "video.video"}
        bad_graphs.append(graph)
        graph = copy.deepcopy(original)
        graph["steps"][2]["params"] = {"num_inference_steps": 7}
        bad_graphs.append(graph)
        graph = copy.deepcopy(original)
        graph["steps"][0]["params"] = {"typo": 1}
        bad_graphs.append(graph)
        graph = copy.deepcopy(original)
        graph["steps"][2]["inputs"]["first_frame"] = {"ref": "missing.image"}
        bad_graphs.append(graph)
        for graph in bad_graphs:
            state.save(workflow_path, graph)
            with self.assertRaises(ValueError):
                self.plan()
        self.assertFalse((self.root / "outputs").exists())

    def test_duplicate_items_and_missing_input_rejected(self):
        task = read_json(self.task_path)
        task["items"].append(task["items"][0])
        state.save(self.task_path, task)
        with self.assertRaises(ValueError):
            self.plan()
        task["items"] = [{"id": "001"}]
        state.save(self.task_path, task)
        with self.assertRaises(ValueError):
            self.plan()

    def test_ambiguous_image_submission_never_retried_blocks_downstream(self):
        directory = self.create()
        self.services.submit_error = TimeoutError("private-secret")
        self.assertEqual(self.execute(directory)["counts"], {"uncertain": 2, "blocked": 1})
        self.services.submit_error = None
        self.execute(directory)
        self.assertEqual(len(self.services.submissions), 2)
        self.assertNotIn("private-secret", (directory / "state.json").read_text())

    def test_process_crash_after_post_before_response_remains_uncertain(self):
        directory = self.create("pika-t2va")
        self.services.submit_error = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.execute(directory)
        record = read_json(directory / "state.json")["jobs"]["video/001"]
        self.assertEqual(record["status"], "submitting")
        self.services.submit_error = None
        self.assertEqual(self.execute(directory)["counts"], {"uncertain": 1})
        self.assertEqual(len(self.services.submissions), 1)

    def test_persisted_id_survives_crash_then_poll_and_download_only(self):
        directory = self.create("pika-t2va")
        self.services.poll_error = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.execute(directory)
        record = read_json(directory / "state.json")["jobs"]["video/001"]
        self.assertEqual(record["job_id"], "remote-001")
        self.services.poll_error = None
        self.assertEqual(self.execute(directory)["counts"], {"completed": 1})
        self.assertEqual(len(self.services.submissions), 1)

    def test_download_failure_recovers_without_generation(self):
        directory = self.create("pika-t2va")
        self.services.download_error = ConnectionError()
        self.assertEqual(self.execute(directory)["counts"], {"uncertain": 1})
        self.services.download_error = None
        self.assertEqual(self.execute(directory)["counts"], {"completed": 1})
        self.assertEqual(len(self.services.submissions), 1)
        self.assertEqual(len(self.services.downloads), 2)

    def test_manifest_recovers_crash_before_state_commit(self):
        directory = self.create("moss-t2i")
        original_save = state.save

        def interrupted_save(path, value):
            if Path(path).name == "state.json" and value["jobs"]["image/001"]["status"] == "completed":
                raise KeyboardInterrupt()
            original_save(path, value)

        with patch.object(state, "save", side_effect=interrupted_save), self.assertRaises(KeyboardInterrupt):
            self.execute(directory)
        self.assertEqual(self.execute(directory)["counts"], {"completed": 1})
        self.assertEqual(len(self.services.submissions), 1)

    def test_corrupt_image_not_reused_or_regenerated(self):
        directory = self.create("moss-t2i")
        self.execute(directory)
        image = directory / "001.png"
        self.assertTrue(image.is_file())
        image.write_bytes(b"corrupted")
        self.assertEqual(self.execute(directory)["counts"], {"uncertain": 1})
        self.assertEqual(len(self.services.submissions), 1)

    def test_unknown_remote_job_is_never_resubmitted(self):
        directory = self.create("pika-t2va")
        self.services.poll_error = RuntimeError("HTTP 404")
        self.execute(directory)
        self.assertEqual(self.execute(directory)["counts"], {"uncertain": 1})
        self.assertEqual(len(self.services.submissions), 1)

    def test_explicit_remote_failure_not_retried(self):
        directory = self.create("pika-t2va")
        self.services.remote_status = "failed"
        self.assertEqual(self.execute(directory)["counts"], {"failed": 1})
        self.execute(directory)
        self.assertEqual(len(self.services.submissions), 1)

    def test_run_lock_and_plan_endpoint_tampering(self):
        directory = self.create()
        with state.lock(directory), self.assertRaises(RuntimeError):
            self.execute(directory)
        with patch.dict(os.environ, {"MOSS_ENDPOINT": "http://different.test"}), self.assertRaises(ValueError):
            self.execute(directory)
        plan = read_json(directory / "plan.json")
        plan["jobs"][0]["inputs"]["seed"] += 1
        state.save(directory / "plan.json", plan)
        with self.assertRaises(ValueError):
            self.execute(directory)
        self.assertEqual(len(self.services.submissions), 0)

    def test_local_input_snapshot_unchanged_after_source_edit(self):
        (self.root / "inputs").mkdir()
        image = self.root / "inputs/frame.png"
        image.write_bytes(PNG)
        workflow = {"version": 1, "id": "frames", "steps": [{
            "id": "video", "uses": "pika.fl2va", "inputs": {
                "prompt": {"ref": "input.prompt"}, "first_frame": {"ref": "input.frame"},
                "last_frame": {"ref": "input.frame"}}}], "outputs": {"video": {"ref": "video.video"}}}
        state.save(self.root / "workflows/frames.json", workflow)
        task = {"version": 1, "name": "001-story-demo", "workflow": "frames", "items": [
            {"id": "001", "prompt": "Ocean", "frame": {"file": "inputs/frame.png"}}]}
        state.save(self.task_path, task)
        directory = self.create()
        image.write_bytes(b"changed")
        self.assertEqual(self.execute(directory)["counts"], {"completed": 1})

    def test_path_escape_rejected(self):
        with self.assertRaises(ValueError):
            state.local_path(self.root, "../escape")
        for value in ("..", "CON", "nul", "a/b"):
            with self.assertRaises(ValueError):
                runner.identifier(value)

    def test_http_clients_auth_and_real_adapter_chain(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, body, mime="application/json"):
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, payload, dict(self.headers)))
                if self.path == "/v1/images/generations":
                    self.reply(json.dumps({"data": [{"b64_json": base64.b64encode(PNG).decode()}]}).encode())
                else:
                    self.reply(b'{"id":"video-job"}')

            def do_GET(self):
                requests.append((self.path, None, dict(self.headers)))
                if self.path.endswith("/content"):
                    self.reply(MP4, "video/mp4")
                else:
                    self.reply(b'{"status":"completed"}')

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            with patch.dict(os.environ, {"MOSS_ENDPOINT": url, "PIKA_ENDPOINT": url}):
                directory = self.create()
                result = runner.execute(directory, timeout=5, poll=0.001)
                self.assertEqual(result["counts"], {"completed": 3}, read_json(directory / "state.json"))
                runner.execute(directory, timeout=5, poll=0.001)
            self.assertEqual(len(requests), 5)
            self.assertEqual(requests[0][2]["Modal-Key"], "private-key")
            self.assertEqual(requests[0][2]["Modal-Secret"], "private-secret")
            self.assertEqual(base64.b64decode(requests[2][1]["first_frame_base64"]), PNG)
            self.assertEqual(requests[-1][0], "/v1/videos/video-job/content")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)

    def test_workflow_edits_do_not_change_existing_run(self):
        directory = self.create()
        workflow_path = self.root / "workflows/storyboard-to-video.json"
        workflow = read_json(workflow_path)
        workflow["steps"][0]["params"] = {"guidance_scale": 7.0}
        state.save(workflow_path, workflow)
        self.execute(directory)
        self.assertEqual(self.services.submissions[0][1]["guidance_scale"], 3.0)
        self.assertEqual(self.plan()["jobs"][0]["params"]["guidance_scale"], 7.0)


if __name__ == "__main__":
    unittest.main()
