"""Offline batch run/resume verification with a fake video API."""
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pika_batch", ROOT / "batch_run.py")
batch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(batch)
VIDEO = b"\x00\x00\x00\x1cftypisom" + (b"mock-mp4-data" * 5)


class BatchTests(unittest.TestCase):
    def test_batch_and_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            workflow = work / "workflow.json"
            task = work / "tasks.json"
            workflow.write_text(json.dumps({
                "mode": "t2va", "seconds": 5, "num_inference_steps": 6,
                "target": {"short_edge": 480, "aspect_ratio": "16:9", "duration_seconds": 5},
                "num_outputs_per_prompt": 1, "seed_start": 1100,
            }), encoding="utf-8")
            task.write_text(json.dumps({
                "name": "test", "items": [
                    {"id": "001", "prompt": "Ocean"},
                    {"id": "002", "prompt": "Mountain"},
                ],
            }), encoding="utf-8")
            submitted = []

            def fake_json(url, payload=None, **_):
                if url.endswith("/v1/videos"):
                    submitted.append(payload)
                    return {"id": str(len(submitted))}
                return {"status": "completed"}

            def fake_bytes(url, **_):
                self.assertTrue(url.endswith("/content"))
                return VIDEO

            with patch("shared.inference_api.request_json", side_effect=fake_json), \
                 patch("shared.inference_api.request_bytes", side_effect=fake_bytes):
                first = batch.run("http://fake", workflow, task, work / "outputs", run_id="test-run")
                self.assertEqual(first["completed"], 2)
                self.assertEqual(first["failed"], 0)
                self.assertEqual([p["seed"] for p in submitted], [1101, 1102])
                second = batch.run("http://fake", workflow, task, work / "outputs", run_id="test-run")
                self.assertEqual(second["skipped"], 2)
                self.assertEqual(len(submitted), 2)
                for name in ("001", "002"):
                    video = work / "outputs" / "test" / "001-main" / f"{name}.mp4"
                    metadata = video.parent / ".runner" / "items" / name / "video" / "metadata.json"
                    self.assertEqual(video.read_bytes(), VIDEO)
                    self.assertEqual(json.loads(metadata.read_text())["sha256"], hashlib.sha256(VIDEO).hexdigest())
                # Changed inputs cannot overwrite an existing scene even when re-invoked.
                content = json.loads(task.read_text())
                content["items"][1]["prompt"] = "Volcanic mountain"
                task.write_text(json.dumps(content), encoding="utf-8")
                with self.assertRaises(ValueError):
                    batch.run("http://fake", workflow, task, work / "outputs", run_id="test-run")
                with self.assertRaises(ValueError):
                    batch.run("http://fake", workflow, task, work / "outputs")
                self.assertEqual(len(submitted), 2)


if __name__ == "__main__":
    unittest.main()
