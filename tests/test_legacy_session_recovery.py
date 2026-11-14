"""CPU-only read-only recovery of old session results; no GPU worker."""
import hashlib
import json
from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.recover_legacy_sessions import recover

PNG = b"\x89PNG\r\n\x1a\n" + b"test-image" * 2000


class Volume:
    def __init__(self, prefix, result):
        self.prefix = prefix
        self.result = result

    def read_file(self, path):
        if path != self.prefix + "/output.png":
            raise FileNotFoundError(path)
        return iter([PNG])


class LegacyRecoveryTests(unittest.TestCase):
    def test_read_only_ledger_and_verified_download(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            directory = root / "outputs/.sessions"
            directory.mkdir(parents=True)
            job = {"id": "old-job", "identity": "003-stop-rain/001-scene/001",
                   "fingerprint": "old-fingerprint", "project": "003-stop-rain",
                   "scene": "001-scene", "shot": "001", "output": "image"}
            result = {"status": "completed", "job_id": job["id"],
                      "fingerprint": job["fingerprint"], "suffix": ".png",
                      "sha256": hashlib.sha256(PNG).hexdigest(), "bytes": len(PNG)}
            with closing(sqlite3.connect(directory / "tasks.sqlite3")) as conn:
                conn.execute("CREATE TABLE jobs(body TEXT, result TEXT, status TEXT, created REAL)")
                conn.execute("INSERT INTO jobs VALUES (?,?,?,?)",
                             (json.dumps(job), json.dumps(result), "completed", 1.0))
                conn.commit()
            remote = "/session-results/" + job["identity"] + "/" + job["fingerprint"]
            volume = Volume(remote, result)
            self.assertEqual(recover(root, fetch=False, volume=volume)["pending"], 1)
            expected = root / "outputs/003-stop-rain/001-scene/001.png"
            self.assertFalse(expected.exists())
            self.assertEqual(recover(root, fetch=True, volume=volume)["downloaded"], 1)
            self.assertEqual(expected.read_bytes(), PNG)
            self.assertEqual(recover(root, fetch=True, volume=volume)["present"], 1)
            with closing(sqlite3.connect(directory / "tasks.sqlite3")) as conn:
                self.assertEqual(conn.execute("SELECT status FROM jobs").fetchone()[0], "completed")


if __name__ == "__main__":
    unittest.main()
