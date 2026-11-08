"""Offline regression for model-agnostic HTTP and resume primitives."""
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from shared.batch import completed_output, counts, intent_digest, safe_id
from shared.files import atomic_json, sha256_file
from shared.http import poll_job, request_bytes, request_json


class FakeResponse:
    def __init__(self, body):
        self.body = body
    def __enter__(self):
        return self
    def __exit__(self, *_):
        return False
    def read(self):
        return self.body


class HTTPBatchTests(unittest.TestCase):
    def test_post_is_not_retried(self):
        with self.assertRaisesRegex(ValueError, "unsafe"):
            request_bytes("http://localhost/", {"prompt": "test"}, attempts=2)
        with patch("shared.http.urllib.request.urlopen",
                   side_effect=urllib.error.URLError("connection lost")) as call:
            with self.assertRaises(urllib.error.URLError):
                request_bytes("http://localhost/v1/videos", {"prompt": "test"})
            self.assertEqual(call.call_count, 1)

    def test_get_retries_then_returns_json(self):
        outcomes = [urllib.error.URLError("temporary"), FakeResponse(b'{"ok":true}')]
        with patch("shared.http.urllib.request.urlopen", side_effect=outcomes) as call, patch(
                "shared.http.time.sleep") as delay:
            response = request_json("http://localhost/health", attempts=2)
            self.assertEqual(response, {"ok": True})
            self.assertEqual(call.call_count, 2)
            delay.assert_called_once()

    def test_permanent_http_error_not_retried(self):
        import io
        exc = urllib.error.HTTPError("http://localhost/", 401, "unauthorized", {}, io.BytesIO(b"denied"))
        with patch("shared.http.urllib.request.urlopen", side_effect=exc) as call:
            with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
                request_bytes("http://localhost/", attempts=3)
            self.assertEqual(call.call_count, 1)

    def test_poll_completion_and_failure(self):
        items = iter(({"status": "queued"}, {"status": "completed"}))
        with patch("shared.http.time.sleep"):
            self.assertEqual(poll_job(lambda: next(items), timeout=10, interval=1)["status"],
                             "completed")
        with self.assertRaisesRegex(RuntimeError, "failed"):
            poll_job(lambda: {"status": "failed"}, timeout=2)

    def test_signature_resume_and_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            mp4 = root / "clip.mp4"
            mp4.write_bytes(b"0000ftyp" + b"content"*10)
            metadata = root / "clip.json"
            sig = intent_digest({"model": "pika", "seed": 5})
            atomic_json(metadata, {"status": "completed", "intent_hash": sig,
                                   "sha256": sha256_file(mp4)})
            self.assertTrue(completed_output(mp4, metadata, sig))
            self.assertFalse(completed_output(mp4, metadata, intent_digest({"seed": 6})))
            mp4.write_bytes(mp4.read_bytes() + b"changed")
            self.assertFalse(completed_output(mp4, metadata, sig))
            self.assertEqual(counts([{"status":"completed"},{"status":"skipped"},
                                     {"status":"failed"}], 5),
                             {"completed":1,"skipped":1,"failed":1,"unattempted":2})
            self.assertEqual(safe_id("clip-01"), "clip-01")
            with self.assertRaises(ValueError):
                safe_id("../danger")


if __name__ == "__main__":
    unittest.main()
