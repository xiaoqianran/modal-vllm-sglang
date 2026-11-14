"""Offline tests for the deployed Pika SDK batch entrypoint."""
import importlib.util
import json
import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.batch import intent_digest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("pika_modal_batch", ROOT / "run.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
import video


class DeployedBatchTests(unittest.TestCase):
    def test_generate_download_and_second_run_skips_gpu(self):
        """Remote metadata must agree with the local resume signature."""
        workflow_path = ROOT / "workflow_t2va.json"
        task_path = ROOT / "tasks/t2va_demo.json"
        workflow = video.read_json(workflow_path)
        task = video.read_json(task_path)
        _, items = video.validate_task(workflow, task, limit=1)
        intent = video.intent_hash(workflow, items[0])
        run_id = "v2-" + intent_digest({
            "model": video.MODEL, "workflow": workflow,
            "name": task["name"], "project": task.get("project"),
            "scene": task.get("scene"), "items": [intent],
        })[:20]
        prefix = f"009-pika-batch/t2va/{task['name']}/{run_id}"
        clip = b"\x00\x00\x00\x18ftypisom" + b"test-video" * 20
        import hashlib
        manifest = b'{"model":"Pika-Labs/Pika-H3-Flex","version":2}'
        manifest_hash = hashlib.sha256(manifest).hexdigest()
        metadata = {"status": "completed", "intent_hash": intent,
                    "model_manifest_sha256": manifest_hash,
                    "execution_hash": video.execution_hash({"intent_hash": intent}, manifest_hash),
                    "sha256": hashlib.sha256(clip).hexdigest()}

        class FakeVolume:
            def __init__(self):
                self.manifest = manifest

            def read_file(self, path):
                value = {
                    f"/{prefix}/001.mp4": clip,
                    f"/{prefix}/001.json": json.dumps(metadata).encode(),
                    "009-pika-h3-flex/model-ready.json": self.manifest,
                }[path]
                return iter([value])

        fake_volume = FakeVolume()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(module, "volume", fake_volume), \
             patch("modal.Cls.from_name") as lookup:
            lookup.return_value.return_value.generate_batch.remote.return_value = {
                "results": [{"id": "001", "status": "completed"}],
                "completed": 1, "failed": 0, "unattempted": 0,
            }
            args = dict(workflow=str(workflow_path), task=str(task_path),
                        limit=1, output=directory)
            module.main(**args)
            local = Path(directory) / task.get("project", task["name"]) / task.get("scene", "001-main")
            self.assertEqual((local / "001.mp4").read_bytes(), clip)
            self.assertEqual(
                video.read_json(local / ".runner/items/001/video/metadata.json")["intent_hash"],
                intent,
            )
            module.main(**args)
            lookup.assert_called_once()
            self.assertEqual((local / "001.mp4").read_bytes(), clip)
            fake_volume.manifest = b"changed-model-manifest"
            with self.assertRaises(FileExistsError):
                module.main(**args)
            lookup.assert_called_once()

    def test_batch_calls_deployed_function(self):
        with tempfile.TemporaryDirectory() as directory, patch("modal.Cls.from_name") as lookup:
            lookup.return_value.return_value.generate_batch.remote.return_value = {
                "results": [], "failed": 0, "unattempted": 0,
            }
            module.main(task=str(ROOT / "tasks/t2va_demo.json"),
                        workflow=str(ROOT / "workflow_t2va.json"),
                        limit=1, output=directory, run_id="demo")
        lookup.assert_called_once_with(module.APP_NAME, "Pika")
        arguments = lookup.return_value.return_value.generate_batch.remote.call_args.args
        self.assertEqual(len(arguments[1]), 1)
        self.assertIn("009-pika-batch/t2va/", arguments[2])

    def test_default_prefix_stable_across_retries(self):
        with tempfile.TemporaryDirectory() as directory, patch("modal.Cls.from_name") as lookup:
            lookup.return_value.return_value.generate_batch.remote.return_value = {
                "results": [], "failed": 0, "unattempted": 0,
            }
            options = dict(task=str(ROOT / "tasks/t2va_demo.json"),
                           workflow=str(ROOT / "workflow_t2va.json"),
                           limit=1, output=directory)
            module.main(**options)
            module.main(**options)
            paths = [call.args[2] for call in
                     lookup.return_value.return_value.generate_batch.remote.call_args_list]
            self.assertEqual(paths[0], paths[1])
            self.assertIn("/v2-", paths[0])

    def test_t2va_config_and_payload(self):
        workflow = module.json_read(ROOT / "workflow_t2va.json")
        task = module.json_read(ROOT / "tasks/t2va_demo.json")
        mode, entries = module.validate_task(workflow, task, limit=1)
        self.assertEqual(mode, "t2va")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["seed"], 1101)
        payload = video._payload(workflow, entries[0])
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
            payload = video._payload(workflow, entries[0])
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
