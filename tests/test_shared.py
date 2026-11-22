"""CPU-only tests for shared services and CPU-to-GPU cache path portability."""
import importlib.util
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from http.client import IncompleteRead

from runtime.config.cache_inventory import file_inventory
from runtime.utils.files import atomic_json, download_volume_file, read_json, sha256_file
from runtime.serving.process_lifecycle import monitor_processes, stop_process, wait_for_health
from runtime.utils.http import request_bytes

class SharedTests(unittest.TestCase):
    def test_truncated_get_retries_but_post_never_retries(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch("runtime.utils.http.urllib.request.urlopen", return_value=response) as call, \
                patch("runtime.utils.http.time.sleep"):
            response.read.side_effect = [IncompleteRead(b"part"), b"complete"]
            self.assertEqual(request_bytes("http://localhost/content", attempts=2), b"complete")
            self.assertEqual(call.call_count, 2)
            call.reset_mock()
            response.read.side_effect = IncompleteRead(b"part")
            with self.assertRaises(IncompleteRead):
                request_bytes("http://localhost/generate", {"prompt": "test"})
            call.assert_called_once()

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
        with patch("runtime.serving.process_lifecycle.urllib.request.urlopen", return_value=resp):
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

    def test_monitor_exits_failed_container_but_not_normal_shutdown(self):
        for shutdown in (False, True):
            with self.subTest(shutdown=shutdown):
                stopping = threading.Event()
                if shutdown:
                    stopping.set()
                process, volume, failed = Mock(), Mock(), Mock()
                process.poll.return_value = 1
                volume.commit.side_effect = OSError("commit unavailable")
                thread = monitor_processes([process], volume, label="test",
                                           stopping=stopping, on_failure=failed)
                thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertEqual(failed.call_count, 0 if shutdown else 1)

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
