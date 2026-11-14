"""Contract tests for SDK batches and the retained HTTP FL2VA adapter."""
import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import video
import proxy

PNG = b"\x89PNG\r\n\x1a\n" + b"mock-image-bytes"


class WorkflowTests(unittest.TestCase):
    def test_video_modes_and_stable_intent_hash(self):
        wf = video.read_json(ROOT / "workflow_t2va.json")
        task = video.read_json(ROOT / "tasks/t2va_demo.json")
        mode, items = video.validate_task(wf, task, limit=1)
        self.assertEqual(mode, "t2va")
        self.assertEqual(items[0]["seed"], wf["seed_start"] + 1)
        self.assertEqual(video._payload(wf, items[0])["conditions"], [])
        self.assertEqual(video.intent_hash(wf, items[0]), video.intent_hash(wf, items[0]))
        different = dict(items[0], seed=items[0]["seed"] + 1)
        self.assertNotEqual(video.intent_hash(wf, items[0]), video.intent_hash(wf, different))

    def test_fl2va_local_files_and_http_inline_frames(self):
        wf = video.read_json(ROOT / "workflow_fl2va.json")
        with tempfile.TemporaryDirectory() as directory:
            first, last = Path(directory) / "first.png", Path(directory) / "last.png"
            first.write_bytes(PNG)
            last.write_bytes(PNG + b"last")
            task = {"name": "frames", "items": [{
                "id": "001", "prompt": "Camera pans",
                "first_frame": str(first), "last_frame": str(last),
            }]}
            _, entries = video.validate_task(wf, task)
            signature = video.intent_hash(wf, entries[0])
            for i, frame in enumerate(entries[0]["frames"]):
                frame["volume_path"] = f"/mnt/modal/input/{i}.png"
            data = video._payload(wf, entries[0])
            self.assertEqual([c["frame_index"] for c in data["conditions"]], [0, -1])
            self.assertTrue(all(c["uri"].startswith("file://") for c in data["conditions"]))
            inline = dict(data, conditions=[],
                          first_frame_base64=base64.b64encode(PNG).decode(),
                          last_frame_base64=base64.b64encode(PNG + b"last").decode())
            previous = proxy.FRAME_DIR
            try:
                proxy.FRAME_DIR = Path(directory) / "staging"
                prepared = proxy.prepare_request(inline)
                self.assertEqual([c["frame_index"] for c in prepared["conditions"]], [0, -1])
            finally:
                proxy.FRAME_DIR = previous
            last.write_bytes(PNG + b"changed")
            _, updated = video.validate_task(wf, task)
            self.assertNotEqual(signature, video.intent_hash(wf, updated[0]))

    def test_rejects_invalid_sampling_and_frames(self):
        workflow = video.read_json(ROOT / "workflow_t2va.json")
        task = {"name": "valid", "items": [{"id": "001", "prompt": "Ocean"}]}
        workflow["num_inference_steps"] = 30
        with self.assertRaises(ValueError):
            video.validate_task(workflow, task)
        with self.assertRaises(ValueError):
            proxy.prepare_request({"task": "fl2va", "num_inference_steps": 6})
        with self.assertRaises(ValueError):
            proxy.prepare_request({"task": "t2va", "num_inference_steps": 6,
                                   "first_frame_base64": "AA=="})


if __name__ == "__main__":
    unittest.main()
