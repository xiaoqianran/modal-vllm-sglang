"""CPU-only MiniMax deployed SDK tests: no Modal compute or model downloads."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "005-minimax-h3-image"))
import image as h3_image

spec = importlib.util.spec_from_file_location(
    "minimax_sdk_runner_under_test", ROOT / "005-minimax-h3-image" / "run.py"
)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

PNG = b"\x89PNG\r\n\x1a\n" + b"test-image" * 128


class FakeVolume:
    def __init__(self):
        self.files = {}
        self.commits = 0

    def read_file(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return iter([self.files[path]])

    def commit(self):
        self.commits += 1


class MiniMaxSdkTests(unittest.TestCase):
    def setUp(self):
        self.volume = FakeVolume()
        self.shot = {"id": "001", "seed": 42, "prompt": "a red paper boat on a lake"}

    def test_prepare_input_is_signed_and_validated(self):
        item = h3_image.prepare(self.shot)
        self.assertEqual(item["payload"]["num_inference_steps"], 6)
        self.assertEqual(item["payload"]["size"], "2048x2048")
        self.assertEqual(item["payload"]["generator_device"], "cpu")
        self.assertEqual(item["payload"]["seed"], 42)
        self.assertEqual(item, h3_image.prepare(self.shot))
        self.assertNotEqual(item["intent_hash"], h3_image.prepare(
            dict(self.shot, prompt="different text"))["intent_hash"])
        for bad in (dict(self.shot, seed=True), dict(self.shot, params={"size": "1024x1024"}),
                    dict(self.shot, id="../escape"), dict(self.shot, params={"num_inference_steps": 5})):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                h3_image.prepare(bad)

    def test_remote_gen_once_then_skip_and_reject_unverified_existing(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(h3_image, "OUTPUT_ROOT", Path(directory)), \
             patch.object(h3_image, "VLLMImageClient") as client:
            client.return_value.generate.return_value = (PNG, ".png")
            prefix = "005-minimax-h3-image/demo/batch-test"
            first = h3_image.generate_batch([self.shot], prefix, volume=self.volume)
            self.assertEqual(first["completed"], 1)
            self.assertEqual(self.volume.commits, 1)
            self.assertEqual(h3_image.generate_batch([self.shot], prefix, volume=self.volume)["skipped"], 1)
            client.return_value.generate.assert_called_once()
            target = Path(directory) / prefix / "001.png"
            self.assertEqual(target.read_bytes(), PNG)
            with self.assertRaises(FileExistsError):
                h3_image.generate_batch([dict(self.shot, prompt="changed")], prefix, volume=self.volume)
            target.write_bytes(b"corrupt")
            with self.assertRaises(FileExistsError):
                h3_image.generate_batch([self.shot], prefix, volume=self.volume)
            with self.assertRaises(ValueError):
                h3_image.generate_batch([self.shot], "../escape", volume=self.volume)

    def test_cli_only_calls_deployed_class_for_missing_remote_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "task.json"
            task.write_text(json.dumps({
                "version": 1, "name": "demo", "workflow": "minimax-t2i",
                "items": [self.shot],
            }), encoding="utf-8")
            _, _, items, prefix = runner.task_plan(task)
            signature = items[0][1]["intent_hash"]

            def remote(items_arg, prefix_arg):
                self.assertEqual(prefix_arg, prefix)
                self.assertEqual(items_arg, [items[0][0]])
                record = {"id": "001", "status": "completed",
                          "file": "001.png", "intent_hash": signature,
                          "sha256": h3_image.sha256_file(self._write_temp(root, PNG))}
                self.volume.files[f"/{prefix}/001.png"] = PNG
                self.volume.files[f"/{prefix}/001.json"] = json.dumps(record).encode()
                return {"total": 1, "completed": 1, "skipped": 0,
                        "failed": 0, "unattempted": 0, "results": [record]}

            with patch.object(runner, "model_volume", return_value=self.volume), \
                 patch("modal.Cls.from_name") as lookup:
                lookup.return_value.return_value.generate_batch.remote.side_effect = remote
                result = runner.run(task, output=root / "outputs")
                self.assertEqual(result["completed"], 1)
                lookup.assert_called_once_with(runner.APP_NAME, "H3")
                result_file = root / "outputs/demo/001-main/001.png"
                self.assertEqual(result_file.read_bytes(), PNG)
                # Identical task is verified locally without a second SDK/GPU request.
                runner.run(task, output=root / "outputs")
                lookup.assert_called_once()
                # Any changed prompt must not overwrite the existing local output.
                task.write_text(json.dumps({
                    "version": 1, "name": "demo", "workflow": "minimax-t2i",
                    "items": [dict(self.shot, prompt="changed")],
                }))
                with self.assertRaises(FileExistsError):
                    runner.run(task, output=root / "outputs")
                lookup.assert_called_once()

    def test_volume_recovery_avoids_gpu_after_client_interruption(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "task.json"
            task.write_text(json.dumps({
                "version": 1, "name": "demo", "workflow": "minimax-t2i",
                "items": [self.shot],
            }), encoding="utf-8")
            _, _, items, prefix = runner.task_plan(task)
            from hashlib import sha256
            record = {"id": "001", "status": "completed",
                      "file": "001.png", "intent_hash": items[0][1]["intent_hash"],
                      "sha256": sha256(PNG).hexdigest()}
            self.volume.files[f"/{prefix}/001.json"] = json.dumps(record).encode()
            self.volume.files[f"/{prefix}/001.png"] = PNG
            with patch.object(runner, "model_volume", return_value=self.volume), \
                 patch("modal.Cls.from_name") as lookup:
                result = runner.run(task, output=root / "outputs")
                self.assertEqual(result["pending"], 0)
                lookup.assert_not_called()
            self.assertEqual((root / "outputs/demo/001-main/001.png").read_bytes(), PNG)

    @staticmethod
    def _write_temp(root, data):
        path = root / "hash-only.png"
        path.write_bytes(data)
        return path


if __name__ == "__main__":
    unittest.main()
