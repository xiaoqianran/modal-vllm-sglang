"""CPU-only tests for shared services and CPU-to-GPU cache path portability."""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from shared.cache import file_inventory
from shared.files import atomic_json, download_volume_file, read_json, sha256_file
from shared.runtime import monitor_processes, stop_process, wait_for_health

MOSS = Path(__file__).resolve().parents[1] / "001-moss-image"
spec = importlib.util.spec_from_file_location("moss_local_cache", MOSS / "model_cache.py")
moss_cache = importlib.util.module_from_spec(spec)
spec.loader.exec_module(moss_cache)


class SharedTests(unittest.TestCase):
    def test_atomic_json_and_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sub" / "report.json"
            atomic_json(path, {"ok": True})
            self.assertEqual(read_json(path), {"ok": True})
            self.assertFalse(path.with_name(path.name + ".tmp").exists())
            self.assertEqual(sha256_file(path), __import__("hashlib").sha256(path.read_bytes()).hexdigest())

    def test_volume_transfer_and_failure_cleanup(self):
        volume = Mock()
        volume.read_file.return_value = iter((b"abc", b"def"))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "video.mp4"
            download_volume_file(volume, "/video.mp4", path)
            self.assertEqual(path.read_bytes(), b"abcdef")
            def interrupted():
                yield b"part"
                raise OSError("interrupted")
            volume.read_file.return_value = interrupted()
            with self.assertRaises(OSError):
                download_volume_file(volume, "/video.mp4", path)
            self.assertEqual(path.read_bytes(), b"abcdef")
            self.assertFalse(path.with_suffix(".mp4.part").exists())

    def test_service_health_and_process_monitor(self):
        p = Mock()
        p.poll.return_value = None
        resp = Mock()
        resp.__enter__ = Mock(return_value=Mock(status=200))
        resp.__exit__ = Mock(return_value=None)
        with patch("shared.runtime.urllib.request.urlopen", return_value=resp):
            wait_for_health(p, "http://127.0.0.1:8000/health", timeout=1)
        p.poll.return_value = 1
        p.returncode = 1
        with self.assertRaisesRegex(RuntimeError, "exited"):
            wait_for_health(p, "http://127.0.0.1:8000/health", timeout=1)
        p.poll.return_value = None
        stop_process(p)
        p.terminate.assert_called_once()
        p.wait.assert_called_once()
        p2 = Mock()
        p2.poll.return_value = 1
        vol = Mock()
        thread = monitor_processes([p2], vol, label="test", poll_interval=0.01)
        thread.join(2)
        self.assertFalse(thread.is_alive())
        vol.commit.assert_called_once()

    def test_model_manifest_relative_to_mount(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo = "OpenMOSS-Team/MOSS-Image-SFT"
            relative = "models--OpenMOSS-Team--MOSS-Image-SFT/snapshots/abcdef"
            snap = root / "hub" / relative
            snap.mkdir(parents=True)
            weight = snap / "model.safetensors"
            weight.write_bytes(b"original-weights")
            (snap / "config.json").write_text("{}", encoding="utf-8")
            inventory = file_inventory(snap)
            record = {"manifest_version": 1, "repo_id": repo,
                      "snapshot_relative": relative, "file_count": len(inventory),
                      "total_bytes": sum(inventory.values()), "inventory": inventory}
            atomic_json(root / "model-ready.json", record)
            with patch.dict(os.environ, {"HF_HOME": str(root)}):
                self.assertEqual(moss_cache.get_ready_model(repo), repo)
                weight.write_bytes(b"changed")
                with self.assertRaisesRegex(RuntimeError, "incomplete"):
                    moss_cache.get_ready_model(repo)
                weight.write_bytes(b"original-weights")
                record["snapshot_relative"] = "../escape"
                atomic_json(root / "model-ready.json", record)
                with self.assertRaisesRegex(RuntimeError, "invalid"):
                    moss_cache.get_ready_model(repo)

    def test_inventory_detects_broken_links(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config.json").write_text("{}", encoding="utf-8")
            self.assertEqual(file_inventory(root), {"config.json": 2})
            try:
                (root / "broken.bin").symlink_to(root / "missing.bin")
            except (OSError, NotImplementedError):
                self.skipTest("Symlinks not enabled")
            with self.assertRaisesRegex(RuntimeError, "Broken"):
                file_inventory(root)


if __name__ == "__main__":
    unittest.main()
