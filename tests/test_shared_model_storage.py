"""Shared V2 weights plus writable manifest: CPU-only regression tests."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from shared.config.azure_cache import attach_shared_hf_snapshots
from shared.config.model_cache import verify_cache, command_argv, prepare_cache
from shared.config.model_spec import validate
from shared.config.storage import shared_storage
from shared.serving.modal_setup import model_mounts


FIXTURE = Path(__file__).parent / "fixtures" / "shared-tiny-pilot.model.json"


class SharedModelStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name)
        self.shared = self.parent / "mounted-shared"
        self.meta = self.parent / "writable-metadata" / "service"
        self.shared.mkdir()
        self.meta.mkdir(parents=True)
        self.spec = validate(json.loads(FIXTURE.read_text(encoding="utf-8")))
        self.revision = self.spec["revision"]
        self.snapshot = self.shared / "models--sshleifer--tiny-gpt2" / "snapshots" / self.revision
        self.snapshot.mkdir(parents=True)
        (self.snapshot / "pytorch_model.bin").write_bytes(b"immutable weights")
        (self.snapshot / "config.json").write_bytes(b'{"type":"tiny"}')
        self.volume = Mock()
        self.mock_shared = patch("shared.config.azure_cache.SHARED_MOUNT", str(self.shared))
        self.mock_verify = patch("shared.config.model_cache.SHARED_MOUNT", str(self.shared))
        self.mock_shared.start()
        self.mock_verify.start()
        self.addCleanup(self.mock_shared.stop)
        self.addCleanup(self.mock_verify.stop)

    def test_adopt_and_verify_without_copying(self):
        first = attach_shared_hf_snapshots(self.spec, self.volume, self.meta)
        self.assertTrue(first["ready"])
        self.assertFalse(first["reused"])
        self.volume.commit.assert_called_once()
        marker = json.loads((self.meta / "model-ready.json").read_text())
        self.assertIn("shared_path", marker["components"]["model"])
        self.assertNotIn("path", marker["components"]["model"])
        paths, fingerprint = verify_cache(self.spec, self.meta)
        self.assertEqual(paths["model"], self.snapshot)
        self.assertEqual(first["fingerprint"], fingerprint)
        self.assertEqual(command_argv(self.spec, paths)[-1], str(self.snapshot))
        second = attach_shared_hf_snapshots(self.spec, self.volume, self.meta)
        self.assertTrue(second["reused"])
        self.volume.commit.assert_called_once()

    def test_reject_modified_shared_weights(self):
        attach_shared_hf_snapshots(self.spec, self.volume, self.meta)
        (self.snapshot / "pytorch_model.bin").write_bytes(b"tampered model")
        with self.assertRaisesRegex(RuntimeError, "changed or incomplete"):
            verify_cache(self.spec, self.meta)

    def test_reject_path_traversal_in_marker(self):
        attach_shared_hf_snapshots(self.spec, self.volume, self.meta)
        marker = self.meta / "model-ready.json"
        record = json.loads(marker.read_text())
        record["components"]["model"]["shared_path"] = "../etc"
        marker.write_text(json.dumps(record))
        with self.assertRaisesRegex(RuntimeError, "Invalid shared model path"):
            verify_cache(self.spec, self.meta)

    def test_preserve_legacy_ready_for_rollback(self):
        legacy_record = {
            "version": 1, "cache_spec": "old",
            "components": {"model": {"path": "hub/old-snapshot"}},
        }
        marker = self.meta / "model-ready.json"
        marker.write_text(json.dumps(legacy_record))
        attach_shared_hf_snapshots(self.spec, self.volume, self.meta, replace=True)
        backup = self.meta / "model-ready.legacy.json"
        self.assertEqual(json.loads(backup.read_text()), legacy_record)
        # Replacing a shared manifest must not replace the first legacy backup.
        attach_shared_hf_snapshots(self.spec, self.volume, self.meta, replace=True)
        self.assertEqual(json.loads(backup.read_text()), legacy_record)
        self.assertEqual(verify_cache(self.spec, self.meta)[0]["model"], self.snapshot)

    def test_shared_prepare_does_not_fallback_to_snapshot_download(self):
        with self.assertRaisesRegex(RuntimeError, "CPU cache missing"):
            prepare_cache(self.spec, self.volume, self.meta)
        with self.assertRaisesRegex(RuntimeError, "scripts/download_models.py"):
            prepare_cache(self.spec, self.volume, self.meta, refresh=True)

    def test_legacy_and_overlay_validation(self):
        self.assertTrue(shared_storage(self.spec))
        overlay = dict(self.spec, cache={"mode": "sglang_overlay"})
        with self.assertRaisesRegex(ValueError, "Overlay"):
            validate(overlay)
        legacy = dict(self.spec)
        legacy.pop("model_storage")
        self.assertFalse(shared_storage(validate(legacy)))

    def test_gpu_mounts_keep_weights_readonly_and_service_writable(self):
        with patch("shared.serving.modal_setup.modal.Volume.from_name") as lookup:
            legacy_volume = Mock(name="writable")
            shared_volume = Mock(name="shared")
            lookup.side_effect = [legacy_volume, shared_volume]
            readonly = Mock(name="readonly")
            shared_volume.with_mount_options.return_value = readonly
            mounts = model_mounts(self.spec)
            self.assertIs(mounts["/mnt/modal"], legacy_volume)
            self.assertIs(mounts["/mnt/models"], readonly)
            shared_volume.with_mount_options.assert_called_once_with(read_only=True)

        with patch("shared.serving.modal_setup.modal.Volume.from_name") as lookup:
            legacy = dict(self.spec, model_storage="legacy")
            self.assertEqual(list(model_mounts(legacy)), ["/mnt/modal"])
            lookup.assert_called_once()


if __name__ == "__main__":
    unittest.main()
