"""Offline checks for declarative extension and submission failure boundaries."""
import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from runtime.utils.files import atomic_json, download_volume_file, read_json
from runtime.serving.model_runtime import create_app
from tests.modal_registration import registrations
from runtime.serving.legacy_cache_preparer import create_download_app, optional_hf_download_secrets
from runtime.serving.inference_execution import execute
from runtime.tasks.task_spec import prepare
from runtime.config.model_spec import discover, parameters, validate
from runtime.tasks.task_lifecycle import SubmissionRejected


SPEC = {"service": "example", "operation": "t2i", "kind": "image", "model": "org/model",
        "revision": "a" * 40, "image": "example/image:tag", "command": ["serve", "{model_path}"],
        "defaults": {"num_inference_steps": 6},
        "options": {"num_inference_steps": {"type": "integer", "enum": [6]}}}


class ModelServiceTests(unittest.TestCase):
    def test_pika_gateway_does_not_share_sglang_broker_port(self):
        from runtime.serving.inference_gateway import PROXY_PORT
        from runtime.config.model_spec import validate
        config = validate(read_json(Path(__file__).resolve().parents[1] / "models/002-pika-h3-flex" / "model.json"))
        self.assertEqual(config["port"], 30010)
        self.assertEqual(config["proxy_port"], 30020)
        self.assertEqual(PROXY_PORT, 30020)
        self.assertNotEqual(config["proxy_port"], config["port"] + 1)

    def test_minimax_http_gateway_uses_dedicated_port(self):
        config = validate(read_json(Path(__file__).resolve().parents[1] / "models/005-minimax-h3-image" / "model.json"))
        self.assertEqual(config["port"], 30000)
        self.assertEqual(config["proxy_port"], 30020)
        self.assertNotEqual(config["proxy_port"], config["port"] + 1)
        self.assertEqual(config["cpu"], 4)
        self.assertEqual(config["memory"], 32768)
    def test_optional_hf_secret_attached_when_present(self):
        import modal
        with patch("modal.Secret.from_name") as lookup:
            secret = lookup.return_value
            self.assertEqual(optional_hf_download_secrets(), [secret])
            lookup.assert_called_once_with("huggingface-secret", required_keys=["HF_TOKEN"])
            secret.hydrate.assert_called_once_with()

    def test_optional_hf_secret_missing_uses_anonymous_download(self):
        import modal
        with patch("modal.Secret.from_name", side_effect=modal.exception.NotFoundError("missing")):
            self.assertEqual(optional_hf_download_secrets(), [])

    def test_legacy_overlay_preparer_rejects_gpu_image_build(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            atomic_json(path, {**SPEC, "cache": {"mode": "sglang_overlay"}})
            with patch("runtime.serving.model_runtime.build_image") as expensive:
                with self.assertRaisesRegex(ValueError, "legacy single-container"):
                    create_download_app(path)
            expensive.assert_not_called()

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
        from tools import new_model
        with tempfile.TemporaryDirectory() as directory, patch.object(new_model, "ROOT", Path(directory)):
            target = new_model.scaffold("006-example", "example", "sglang", "image",
                                        "org/model", "a" * 40, "example/image:tag")
            self.assertIn("example.t2i", discover(directory))
            self.assertIn("Model", runpy.run_path(str(target / "app.py")))
            self.assertIn("download_main", runpy.run_path(str(target / "download_model.py")))
            self.assertFalse((target / "run.py").exists())
            self.assertTrue((Path(directory) / "workflows/example-t2i.json").is_file())
            from pipeline import s01_build_plan as runner
            from orchestration import model_capabilities as adapters
            specs = discover(directory)
            capabilities = {key: {"service": spec["service"], "required": spec["inputs"], "output": spec["kind"]}
                            for key, spec in specs.items()}
            with patch("pipeline.s01_build_plan.ROOT", Path(directory)), patch.dict(adapters.MODEL_SPECS, specs, clear=True), patch.dict(adapters.CAPABILITIES, capabilities, clear=True):
                plan = runner.build_plan(target / "006-example.json", root=Path(directory))
            self.assertEqual(plan["jobs"][0]["uses"], "example.t2i")
            with self.assertRaises(FileExistsError):
                new_model.scaffold("006-example", "example", "sglang", "image",
                                   "org/model", "a" * 40, "example/image:tag")

    def test_scaffold_defaults_to_native_modal_image(self):
        from tools import new_model
        with tempfile.TemporaryDirectory() as directory, patch.object(new_model, "ROOT", Path(directory)):
            with self.assertRaises(ValueError):
                new_model.scaffold("006-invalid", "invalid", "vllm", "image", "org/model", "main")
            self.assertFalse((Path(directory) / "models/006-invalid").exists())
            target = new_model.scaffold("006-native", "native", "vllm", "image", "org/model", "main",
                                        uv_packages=["vllm==0.32.0", "vllm-omni==0.32.0"],
                                        uv_torch_backend="cu130")
            spec = discover(directory)["native.t2i"]
            self.assertEqual(spec["image"], "debian_slim")
            self.assertEqual(spec["python_version"], "3.12")
            self.assertEqual(spec["uv_torch_backend"], "cu130")
            self.assertEqual(spec["memory_snapshot"], "off")
            self.assertEqual(spec["uv_packages"], ["vllm==0.32.0", "vllm-omni==0.32.0"])
            self.assertTrue((target / "app.py").is_file())

    def test_discovery_validation_and_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models/model/model.json"
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
            with registrations() as (classes, functions):
                app, model = create_app(path)
                with patch("runtime.serving.legacy_cache_preparer.optional_hf_download_secrets", return_value=[]):
                    download_app, download, check_cache = create_download_app(path)
            self.assertIs(classes[(app, "Model")], model)
            self.assertIs(functions[(download_app, "download")], download)
            self.assertIs(functions[(download_app, "check_cache")], check_cache)

    def test_lazy_recovery_bypasses_engine_and_checks_media(self):
        from runtime.tasks.task_spec import execution_signature
        item = {"id": "001", "prompt": "boat", "seed": 42}
        payload, signature = prepare(SPEC, item)
        data = b"\x89PNG\r\n\x1a\n" + b"content"
        for status in ("completed", "collecting"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory, \
                    patch("runtime.serving.inference_execution.Path", wraps=Path) as paths:
                paths.side_effect = lambda value: Path(directory) if value == "/data" else Path(value)
                folder = Path(directory) / "batch"
                folder.mkdir()
                (folder / "001.png").write_bytes(data)
                atomic_json(folder / "001.json", {"id": "001", "file": "001.png",
                    "status": status, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                    "intent_hash": signature, "execution_hash": execution_signature(signature, "fingerprint")})
                ensure = Mock(side_effect=AssertionError("Unexpected engine startup"))
                volume = Mock()
                result = list(execute(SPEC, [item], "batch", volume, "fingerprint", ensure_engine=ensure))
                self.assertEqual(result[0]["status"], "completed")
                ensure.assert_not_called()
                self.assertEqual(volume.commit.call_count, int(status == "collecting"))
                (folder / "001.png").write_bytes(b"corrupt")
                with self.assertRaisesRegex(ValueError, "damaged"):
                    list(execute(SPEC, [item], "batch", volume, "fingerprint", ensure_engine=ensure))
                ensure.assert_not_called()

    def test_lazy_startup_failure_never_writes_submitting_checkpoint(self):
        item = {"id": "001", "prompt": "boat", "seed": 42}
        with tempfile.TemporaryDirectory() as directory, patch("runtime.serving.inference_execution.Path", wraps=Path) as paths:
            paths.side_effect = lambda value: Path(directory) if value == "/data" else Path(value)
            ensure = Mock(side_effect=RuntimeError("engine failed"))
            volume = Mock()
            with self.assertRaisesRegex(RuntimeError, "engine failed"):
                list(execute(SPEC, [item], "batch", volume, ensure_engine=ensure))
            self.assertFalse((Path(directory) / "batch" / "001.json").exists())
            volume.commit.assert_not_called()

    def test_lost_image_response_is_not_resubmitted(self):
        volume = Mock()
        item = {"id": "001", "prompt": "boat", "seed": 42}
        with tempfile.TemporaryDirectory() as directory, patch("runtime.serving.inference_execution.Path", wraps=Path) as paths:
            # Redirect only the remote mount, leaving all other Path behavior intact.
            paths.side_effect = lambda value: Path(directory) if value == "/data" else Path(value)
            with patch("runtime.serving.inference_api.ImageGenerationClient") as factory:
                factory.return_value.generate.side_effect = TimeoutError("lost response")
                with self.assertRaises(TimeoutError):
                    list(execute(SPEC, [item], "batch", volume))
                with self.assertRaisesRegex(RuntimeError, "Uncertain"):
                    list(execute(SPEC, [item], "batch", volume))
                self.assertEqual(factory.return_value.generate.call_count, 1)

    def test_video_resumes_same_job_after_download_failure(self):
        spec = {**SPEC, "kind": "video", "operation": "t2v"}
        item = {"id": "001", "prompt": "boat", "seed": 42}
        with tempfile.TemporaryDirectory() as directory, patch("runtime.serving.inference_execution.Path", wraps=Path) as paths:
            paths.side_effect = lambda value: Path(directory) if value == "/data" else Path(value)
            with patch("runtime.serving.inference_api.SGLangVideoClient") as factory:
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
        with tempfile.TemporaryDirectory() as directory, patch("runtime.serving.inference_execution.Path", wraps=Path) as paths:
            paths.side_effect = lambda value: Path(directory) if value == "/data" else Path(value)
            with patch("runtime.serving.inference_api.ImageGenerationClient") as factory:
                factory.return_value.generate.side_effect = [SubmissionRejected(), (b"\x89PNG\r\n\x1a\n" + b"x" * 32, ".png")]
                with self.assertRaises(SubmissionRejected):
                    list(execute(SPEC, [item], "batch", Mock()))
                result = list(execute(SPEC, [item], "batch", Mock()))
                self.assertEqual(result[0]["status"], "completed")
                factory.assert_called_with("http://127.0.0.1:8001")

    def test_sdk_records_terminal_failure_and_does_not_resume_it(self):
        spec = {**SPEC, "kind": "video", "operation": "t2v"}
        item = {"id": "001", "prompt": "boat", "seed": 42}
        with tempfile.TemporaryDirectory() as directory, patch("runtime.serving.inference_execution.Path", wraps=Path) as paths:
            paths.side_effect = lambda value: Path(directory) if value == "/data" else Path(value)
            with patch("runtime.serving.inference_api.SGLangVideoClient") as factory:
                client = factory.return_value
                client.submit.return_value = "job-1"
                def failed(job, *, timeout, poll, on_status):
                    on_status({"status": "failed"})
                    raise RuntimeError("Remote failed")
                client.wait.side_effect = failed
                with self.assertRaises(RuntimeError):
                    list(execute(spec, [item], "batch", Mock()))
                with self.assertRaisesRegex(RuntimeError, "Remote task failed"):
                    list(execute(spec, [item], "batch", Mock()))
                client.submit.assert_called_once()
                client.wait.assert_called_once()
