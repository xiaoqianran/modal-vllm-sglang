"""Offline checks for declarative extension and submission failure boundaries."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from shared.files import atomic_json, download_volume_file
from shared.model_service import create_app, create_download_app, execute, prepare
from shared.model_spec import discover, parameters, validate
from shared.task_lifecycle import SubmissionRejected


SPEC = {"service": "example", "operation": "t2i", "kind": "image", "model": "org/model",
        "revision": "a" * 40, "image": "example/image:tag", "command": ["serve", "{model_path}"],
        "defaults": {"num_inference_steps": 6},
        "options": {"num_inference_steps": {"type": "integer", "enum": [6]}}}


class ModelServiceTests(unittest.TestCase):
    def test_optional_parameter_maps_are_normalized_without_mutating_input(self):
        minimal = {key: value for key, value in SPEC.items() if key not in {"defaults", "options"}}
        original = copy.deepcopy(minimal)
        normalized = validate(minimal)
        self.assertEqual(minimal, original)
        self.assertEqual(normalized["defaults"], {})
        self.assertEqual(normalized["options"], {})
        self.assertEqual(parameters(minimal, {}), {"model": SPEC["model"]})
        with self.assertRaises(ValueError):
            parameters(normalized, {"num_inference_steps": 6})
        item = {"id": "001", "prompt": "boat", "seed": 42}
        explicit = validate({**minimal, "defaults": {}, "options": {}})
        self.assertEqual(prepare(normalized, item), prepare(explicit, item))
        for field in ("defaults", "options"):
            for invalid in (None, [], ""):
                with self.subTest(field=field, value=invalid), self.assertRaises(ValueError):
                    validate({**minimal, field: invalid})

    def test_revision_rules_apply_to_model_and_components(self):
        for revision in ("a" * 40, "main", "release/v1", "v1.0"):
            self.assertEqual(validate({**SPEC, "revision": revision})["revision"], revision)
        for revision in (None, "", " main", "main\n", "bad ref", "REPLACE_WITH_COMMIT_BRANCH_OR_TAG"):
            with self.subTest(revision=revision):
                with self.assertRaises(ValueError):
                    validate({**SPEC, "revision": revision})
                with self.assertRaises(ValueError):
                    validate({**SPEC, "components": {"encoder": {"model": "org/encoder", "revision": revision}}})

    def test_generated_entrypoints_and_discovery(self):
        import runpy
        from scripts import new_model
        with tempfile.TemporaryDirectory() as directory, patch.object(new_model, "ROOT", Path(directory)):
            target = new_model.scaffold("006-example", "example", "sglang", "image",
                                        "org/model", "a" * 40, "example/image:tag")
            self.assertIn("example.t2i", discover(directory))
            self.assertIn("Model", runpy.run_path(str(target / "app.py")))
            self.assertIn("download", runpy.run_path(str(target / "download_model.py")))
            with patch("sys.argv", ["run.py", "--task", str(target / "task.example.json"), "--dry-run"]):
                runpy.run_path(str(target / "run.py"), run_name="__main__")
            with self.assertRaises(FileExistsError):
                new_model.scaffold("006-example", "example", "sglang", "image",
                                   "org/model", "a" * 40, "example/image:tag")

    def test_discovery_validation_and_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model" / "model.json"
            atomic_json(path, SPEC)
            self.assertEqual(discover(directory)["example.t2i"], {**SPEC, "inputs": {"prompt": "string"}})
        for supplied in ({"unknown": 1}, {"num_inference_steps": True}, {"num_inference_steps": 5}):
            with self.assertRaises(ValueError):
                parameters(SPEC, supplied)
        item = {"id": "001", "prompt": "boat", "seed": 42}
        changed = copy.deepcopy(SPEC)
        changed["revision"] = "b" * 40
        self.assertNotEqual(prepare(SPEC, item)[1], prepare(changed, item)[1])

    def test_modal_registration_without_gpu(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            atomic_json(path, SPEC)
            app, model = create_app(path)
            self.assertIn("Model", app.registered_classes)
            download_app, download, check_cache = create_download_app(path)
            self.assertIn("download", download_app.registered_functions)

    def test_lost_image_response_is_not_resubmitted(self):
        volume = Mock()
        item = {"id": "001", "prompt": "boat", "seed": 42}
        with tempfile.TemporaryDirectory() as directory, patch("shared.model_service.Path", wraps=Path) as paths:
            # Redirect only the remote mount, leaving all other Path behavior intact.
            paths.side_effect = lambda value: Path(directory) if value == "/mnt/modal" else Path(value)
            with patch("shared.inference_api.VLLMImageClient") as factory:
                factory.return_value.generate.side_effect = TimeoutError("lost response")
                with self.assertRaises(TimeoutError):
                    list(execute(SPEC, [item], "batch", volume))
                with self.assertRaisesRegex(RuntimeError, "Uncertain"):
                    list(execute(SPEC, [item], "batch", volume))
                self.assertEqual(factory.return_value.generate.call_count, 1)

    def test_video_resumes_same_job_after_download_failure(self):
        spec = {**SPEC, "kind": "video", "operation": "t2v"}
        item = {"id": "001", "prompt": "boat", "seed": 42}
        with tempfile.TemporaryDirectory() as directory, patch("shared.model_service.Path", wraps=Path) as paths:
            paths.side_effect = lambda value: Path(directory) if value == "/mnt/modal" else Path(value)
            with patch("shared.inference_api.SGLangVideoClient") as factory:
                client = factory.return_value
                client.submit.return_value = "job-1"
                client.content.side_effect = [OSError("disconnect"), b"\0\0\0\x20ftyp" + b"x" * 32]
                with self.assertRaises(OSError):
                    list(execute(spec, [item], "batch", Mock()))
                result = list(execute(spec, [item], "batch", Mock()))
                self.assertEqual(result[0]["status"], "completed")
                client.submit.assert_called_once()
                self.assertEqual(client.wait.call_args.args[0], "job-1")

    def test_bad_download_never_replaces_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "image.png"
            target.write_bytes(b"original")
            volume = Mock()
            volume.read_file.return_value = [b"bad"]
            with self.assertRaises(ValueError):
                download_volume_file(volume, "remote", target, expected_sha256="wrong")
            self.assertEqual(target.read_bytes(), b"original")

    def test_busy_sdk_task_can_retry_without_changing_identity(self):
        item = {"id": "001", "prompt": "boat", "seed": 42}
        with tempfile.TemporaryDirectory() as directory, patch("shared.model_service.Path", wraps=Path) as paths:
            paths.side_effect = lambda value: Path(directory) if value == "/mnt/modal" else Path(value)
            with patch("shared.inference_api.VLLMImageClient") as factory:
                factory.return_value.generate.side_effect = [SubmissionRejected(), (b"\x89PNG\r\n\x1a\n" + b"x" * 32, ".png")]
                with self.assertRaises(SubmissionRejected):
                    list(execute(SPEC, [item], "batch", Mock()))
                result = list(execute(SPEC, [item], "batch", Mock()))
                self.assertEqual(result[0]["status"], "completed")
                factory.assert_called_with("http://127.0.0.1:8001")

    def test_sdk_records_terminal_failure_and_does_not_resume_it(self):
        spec = {**SPEC, "kind": "video", "operation": "t2v"}
        item = {"id": "001", "prompt": "boat", "seed": 42}
        with tempfile.TemporaryDirectory() as directory, patch("shared.model_service.Path", wraps=Path) as paths:
            paths.side_effect = lambda value: Path(directory) if value == "/mnt/modal" else Path(value)
            with patch("shared.inference_api.SGLangVideoClient") as factory:
                client = factory.return_value
                client.submit.return_value = "job-1"
                def failed(job, *, timeout, on_status):
                    on_status({"status": "failed"})
                    raise RuntimeError("Remote failed")
                client.wait.side_effect = failed
                with self.assertRaises(RuntimeError):
                    list(execute(spec, [item], "batch", Mock()))
                with self.assertRaisesRegex(RuntimeError, "Remote task failed"):
                    list(execute(spec, [item], "batch", Mock()))
                client.submit.assert_called_once()
                client.wait.assert_called_once()
