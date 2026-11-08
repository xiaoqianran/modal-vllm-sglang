"""不访问网络、不分配 GPU 的模型物化清单校验测试。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import model_cache


class ModelCacheTests(unittest.TestCase):
    def test_missing_marker_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(model_cache, "READY_FILE", Path(directory) / "absent.json"):
                with self.assertRaisesRegex(RuntimeError, "CPU"):
                    model_cache.get_ready_model()

    def test_incomplete_and_modified_snapshot_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sglang" / "materialized_models"
            snapshot = root / "Pika-H3-Flex-check"
            snapshot.mkdir(parents=True)
            (snapshot / "model_index.json").write_text('{"_class_name":"PikaH3FlexPipeline"}')
            (snapshot / ".sglang_overlay_materialized.json").write_text('{"ready":true}')
            (snapshot / "transformer").mkdir()
            weight = snapshot / "transformer" / "model.safetensors"
            weight.write_bytes(b"fake model")
            ready = Path(directory) / "model-ready.json"

            with patch.object(model_cache, "READY_FILE", ready), patch.object(
                model_cache, "MATERIALIZED_ROOT", root
            ):
                inventory = model_cache.inspect_snapshot(snapshot)
                record = {
                    "manifest_version": model_cache.MANIFEST_VERSION,
                    "repo_id": model_cache.MODEL,
                    "materialized_path": str(snapshot),
                    **inventory,
                }
                ready.write_text(json.dumps(record))
                self.assertEqual(model_cache.get_ready_model(), model_cache.MODEL)
                weight.write_bytes(b"corrupt")
                with self.assertRaisesRegex(RuntimeError, "incomplete"):
                    model_cache.get_ready_model()
                weight.unlink()
                with self.assertRaisesRegex(RuntimeError, "No model weights"):
                    model_cache.get_ready_model()

    def test_rejects_broken_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "model_index.json").write_text("{}")
            (root / ".sglang_overlay_materialized.json").write_text("{}")
            (root / "weight.safetensors").write_bytes(b"weights")
            try:
                (root / "broken.safetensors").symlink_to(root / "missing")
            except (OSError, NotImplementedError):
                self.skipTest("当前操作系统不允许创建符号链接")
            with self.assertRaisesRegex(RuntimeError, "Broken"):
                model_cache.inspect_snapshot(root)


if __name__ == "__main__":
    unittest.main()
