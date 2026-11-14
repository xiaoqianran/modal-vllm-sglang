"""Video output integrity: deterministic input hashes and never overwrite old clips."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import video

MP4 = b"\x00\x00\x00\x18ftypisom" + b"mock-video" * 20


class FakeVolume:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.workflow = video.read_json(ROOT / "workflow_t2va.json")
        task = video.read_json(ROOT / "tasks/t2va_demo.json")
        _, self.items = video.validate_task(self.workflow, task, limit=1)
        self.items[0]["intent_hash"] = video.intent_hash(self.workflow, self.items[0])
        self.prefix = "009-pika-batch/t2va/test/v2-123"

    def call(self, volume, manifest="manifest-hash"):
        return video.generate_batch(
            self.workflow, self.items, self.prefix,
            volume=volume, model_manifest_sha256=manifest,
            startup_seconds=12.5,
        )

    def test_generates_once_and_reuses_verified_result(self):
        with tempfile.TemporaryDirectory() as directory,              patch.object(video, "VOLUME_ROOT", directory),              patch.object(video, "SGLangVideoClient") as client:
            client.return_value.submit.return_value = "video-1"
            client.return_value.content.return_value = MP4
            vol = FakeVolume()
            report = self.call(vol)
            self.assertEqual(report["completed"], 1)
            self.assertEqual(report["startup_seconds"], 12.5)
            saved = Path(directory) / self.prefix / "001.mp4"
            metadata = json.loads(saved.with_suffix(".json").read_text())
            self.assertEqual(saved.read_bytes(), MP4)
            self.assertEqual(metadata["intent_hash"], self.items[0]["intent_hash"])
            self.assertNotEqual(metadata["execution_hash"], metadata["intent_hash"])
            self.assertEqual(self.call(vol)["skipped"], 1)
            client.return_value.submit.assert_called_once()
            self.assertGreater(vol.commits, 0)

    def test_rejects_changed_intent_or_manifest_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory,              patch.object(video, "VOLUME_ROOT", directory),              patch.object(video, "SGLangVideoClient") as client:
            client.return_value.submit.return_value = "video-1"
            client.return_value.content.return_value = MP4
            vol = FakeVolume()
            self.call(vol)
            self.items[0]["intent_hash"] = "different-but-valid-hash"
            with self.assertRaises(FileExistsError):
                self.call(vol)
            self.items[0]["intent_hash"] = video.intent_hash(self.workflow, self.items[0])
            with self.assertRaises(FileExistsError):
                self.call(vol, manifest="different-manifest")
            self.assertEqual((Path(directory) / self.prefix / "001.mp4").read_bytes(), MP4)
            client.return_value.submit.assert_called_once()

    def test_rejects_unverified_existing_video_without_submission(self):
        with tempfile.TemporaryDirectory() as directory,              patch.object(video, "VOLUME_ROOT", directory),              patch.object(video, "SGLangVideoClient") as client:
            dest = Path(directory) / self.prefix
            dest.mkdir(parents=True)
            (dest / "001.mp4").write_bytes(MP4)
            with self.assertRaises(FileExistsError):
                self.call(FakeVolume())
            client.return_value.submit.assert_not_called()

    def test_rejects_legacy_mismatched_metadata_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory,              patch.object(video, "VOLUME_ROOT", directory),              patch.object(video, "SGLangVideoClient") as client:
            dest = Path(directory) / self.prefix
            dest.mkdir(parents=True)
            (dest / "001.mp4").write_bytes(MP4)
            (dest / "001.json").write_text(json.dumps({
                "status": "completed", "intent_hash": "legacy-execution-hash",
            }))
            with self.assertRaises(FileExistsError):
                self.call(FakeVolume())
            client.return_value.submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
