"""Offline tests for the independent modal-run entrypoint."""
import importlib.util
import tempfile
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("pika_modal_batch", ROOT / "run_batch.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ModalRunTests(unittest.TestCase):
    def test_t2va_config_and_payload(self):
        workflow = module.json_read(ROOT / "workflow_t2va.json")
        task = module.json_read(ROOT / "tasks/t2va_demo.json")
        mode, entries = module.validate_task(workflow, task, limit=1)
        self.assertEqual(mode, "t2va")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["seed"], 1101)
        payload = module._payload(workflow, entries[0])
        self.assertEqual(payload["num_inference_steps"], 6)
        self.assertEqual(payload["conditions"], [])

    def test_fl2va_images_and_hash_invalidation(self):
        workflow = module.json_read(ROOT / "workflow_fl2va.json")
        with tempfile.TemporaryDirectory() as folder:
            first = Path(folder) / "first.png"
            last = Path(folder) / "last.png"
            first.write_bytes(b"\x89PNG\r\n\x1a\nfirst-frame")
            last.write_bytes(b"\x89PNG\r\n\x1a\nlast-frame")
            task = {"name": "demo", "items": [{
                "id": "001", "prompt": "Realistic camera movement",
                "first_frame": str(first), "last_frame": str(last)
            }]}
            mode, entries = module.validate_task(workflow, task)
            self.assertEqual(mode, "fl2va")
            before = module.intent_hash(workflow, entries[0])
            self.assertEqual(len(entries[0]["frames"]), 2)
            for i, frame in enumerate(entries[0]["frames"]):
                frame["volume_path"] = f"/mnt/modal/009-pika-batch/inputs/{i}.png"
            payload = module._payload(workflow, entries[0])
            self.assertEqual([c["frame_index"] for c in payload["conditions"]], [0, -1])
            self.assertTrue(all(c["uri"].startswith("file://") for c in payload["conditions"]))
            last.write_bytes(b"\x89PNG\r\n\x1a\nmodified-last")
            _, updated = module.validate_task(workflow, task)
            self.assertNotEqual(before, module.intent_hash(workflow, updated[0]))

    def test_rejects_unsafe_and_non_six_step_config(self):
        workflow = module.json_read(ROOT / "workflow_t2va.json")
        task = {"name": "../unsafe", "items": [{"id": "001", "prompt": "test"}]}
        with self.assertRaises(ValueError):
            module.validate_task(workflow, task)
        task["name"] = "safe"
        workflow["num_inference_steps"] = 20
        with self.assertRaises(ValueError):
            module.validate_task(workflow, task)


if __name__ == "__main__":
    unittest.main()
