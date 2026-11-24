import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image
from runtime.config.model_spec import validate
from runtime.serving.snapshot_warmup import warm_image_engine
from runtime.utils.files import read_json
from tools.run_image_batch import run_batch

SPEC = validate(read_json(Path(__file__).resolve().parents[1] / "models/005-minimax-h3-image/model.json"))


class ImageBatchTests(unittest.TestCase):
    def test_all_jobs_queued_before_any_collection_and_no_replay(self):
        items = [{"id": f"{i:03}", "prompt": "mountain lake", "seed": i} for i in range(3)]
        backend = Mock()
        backend.fingerprint.return_value = "cache"
        buffer = io.BytesIO()
        Image.new("RGB", (2, 2)).save(buffer, "PNG")
        data = buffer.getvalue()
        backend.volume.read_file.return_value = [data]
        backend.submit.side_effect = [f"call-{x['id']}" for x in items]
        def get_call(call_id):
            self.assertEqual(backend.submit.call_count, len(items))
            from runtime.tasks.task_spec import prepare, execution_signature
            item = next(x for x in items if call_id.endswith(x["id"]))
            sig = prepare(SPEC, item)[1]
            return SimpleNamespace(get=lambda: {
                "id": item["id"], "file": item["id"]+".png", "status": "completed",
                "intent_hash": sig, "execution_hash": execution_signature(sig, "cache"),
                "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                "execution_seconds": 1.0})
        backend.modal.FunctionCall.from_id.side_effect = get_call
        with tempfile.TemporaryDirectory() as directory, patch("tools.run_image_batch.remote_record", return_value=None):
            result = run_batch(backend, SPEC, items, directory, "test-batch")
            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(result["results"]), 3)
            with self.assertRaises(FileExistsError):
                run_batch(backend, SPEC, items, directory, "test-batch")
            self.assertEqual(backend.submit.call_count, 3)

    def test_warmup_uses_real_shape_and_allows_snapshot_off(self):
        self.assertEqual(SPEC["scaledown_window"], 60)
        with patch("runtime.serving.snapshot_warmup.ImageGenerationClient") as client:
            client.return_value.generate.return_value = (b"test", ".png")
            warm_image_engine(SPEC, timeout=100)
            payload = client.return_value.generate.call_args.args[0]
            self.assertEqual(payload["size"], "2048x1152")
            self.assertEqual(payload["num_inference_steps"], 6)
            self.assertEqual(client.return_value.generate.call_args.kwargs["timeout"], 100)
        self.assertEqual(validate({**SPEC, "memory_snapshot": "off"})["memory_snapshot"], "off")
        with self.assertRaisesRegex(ValueError, "snapshot_warmup"):
            validate({**SPEC, "snapshot_warmup": {"invalid": True}})



    def test_capture_calls_warmup_before_capture_identity(self):
        from runtime.serving.model_runtime import SnapshotModelRuntime
        instance = object.__new__(SnapshotModelRuntime)
        instance._load_spec = Mock(return_value=SPEC)
        with patch("runtime.serving.model_runtime.verify_cache", return_value=({}, "cache")), \
             patch("runtime.serving.model_runtime.command_argv", return_value=["engine"]), \
             patch("runtime.serving.model_runtime.subprocess.Popen"), \
             patch("runtime.serving.process_lifecycle.wait_for_health"), \
             patch("runtime.serving.snapshot_warmup.warm_image_engine") as warm:
            def check(*args, **kwargs):
                self.assertFalse(hasattr(instance, "snapshot_capture_id"))
            warm.side_effect = check
            # Modal wraps lifecycle hooks as partial functions; call the raw hook.
            hook = SnapshotModelRuntime.__dict__["capture_engine"]
            hook._get_raw_f()(instance)
            warm.assert_called_once()
            self.assertEqual(len(instance.snapshot_capture_id), 32)
