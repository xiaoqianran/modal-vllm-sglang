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
