"""Offline metadata-only maintenance; never invokes Modal or downloads."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from runtime.config.model_cache import component_specs, inspect_or_migrate_cache
from runtime.config.model_spec import validate
from runtime.tasks.task_identity import intent_digest
from runtime.utils.files import atomic_json


class CacheMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.spec = validate({
            "service": "example", "operation": "t2i", "kind": "image",
            "model": "org/model", "revision": "a" * 40,
            "image": "example:image", "command": ["serve", "{model_path}"],
        })

    def test_check_and_migrate_reuse_existing_marker_without_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = root / "weights"
            model.mkdir()
            (model / "weight.safetensors").write_bytes(b"content")
            atomic_json(root / "model-ready.json", {
                "version": 1,
                "cache_spec": intent_digest(component_specs(self.spec)),
                "components": {"model": {
                    "path": "weights", "resolved_revision": "a" * 40,
                    "inventory": {"weight.safetensors": 7},
                }},
            })
            volume = Mock()
            with patch("runtime.config.model_cache.cache_root", return_value=root):
                checked = inspect_or_migrate_cache(self.spec, volume)
                migrated = inspect_or_migrate_cache(self.spec, volume, migrate_only=True)
            self.assertTrue(checked["ready"])
            self.assertFalse(migrated["migrated"])
            self.assertEqual(checked["fingerprint"], migrated["fingerprint"])
            volume.commit.assert_not_called()

    def test_migrate_missing_cache_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            volume = Mock()
            with (patch("runtime.config.model_cache.cache_root", return_value=Path(tmp)),
                  self.assertRaisesRegex(RuntimeError, "refusing download")):
                inspect_or_migrate_cache(self.spec, volume, migrate_only=True)
            volume.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
