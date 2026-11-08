"""Offline contract tests; no GPU, proxy token, or network required."""
import base64
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


batch = load("batch_run")
proxy = load("proxy")
PNG = b"\x89PNG\r\n\x1a\n" + b"generated-test-bytes"


class WorkflowTests(unittest.TestCase):
    def test_two_templates_and_first_last(self):
        t2va = batch.read_json(ROOT / "workflow_t2va.json")
        fl2va = batch.read_json(ROOT / "workflow_fl2va.json")
        task = batch.read_json(ROOT / "tasks/t2va_demo.json")
        payload, digest = batch.build_payload(t2va, task["items"][0], 1)
        self.assertEqual(payload["task"], "t2va")
        self.assertEqual(payload["num_inference_steps"], 6)
        self.assertFalse(payload["conditions"])
        self.assertEqual(len(digest), 64)
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.png"
            last = Path(directory) / "last.png"
            first.write_bytes(PNG)
            last.write_bytes(PNG + b"-last")
            item = {"id": "001", "prompt": "Camera pans", "first_frame": str(first), "last_frame": str(last)}
            payload, digest = batch.build_payload(fl2va, item, 1)
            self.assertEqual(payload["task"], "fl2va")
            self.assertEqual(len(payload["first_frame_base64"]), len(base64.b64encode(PNG)))
            proxy.FRAME_DIR = Path(directory) / "staged"
            prepared = proxy.prepare_request(payload)
            self.assertEqual([c["frame_index"] for c in prepared["conditions"]], [0, -1])
            self.assertTrue(all(c["role"] == "keyframe" for c in prepared["conditions"]))
            self.assertTrue(all(c["uri"].startswith("file://") for c in prepared["conditions"]))
            self.assertFalse(any(k.endswith("_base64") for k in prepared))
            self.assertEqual(len(digest), 64)
            first.write_bytes(PNG + b"changed")
            _, changed = batch.build_payload(fl2va, item, 1)
            self.assertNotEqual(changed, digest)

    def test_validation(self):
        template = batch.read_json(ROOT / "workflow_t2va.json")
        item = {"id": "001", "prompt": "A test"}
        template["num_inference_steps"] = 50
        with self.assertRaises(ValueError):
            batch.build_payload(template, item, 1)
        with self.assertRaises(ValueError):
            proxy.prepare_request({"task": "fl2va", "num_inference_steps": 6})
        with self.assertRaises(ValueError):
            proxy.prepare_request({"task": "t2va", "num_inference_steps": 6, "first_frame_base64": "AA=="})
        with self.assertRaises(ValueError):
            proxy._decode_frame(base64.b64encode(b"invalid").decode())


if __name__ == "__main__":
    unittest.main()
