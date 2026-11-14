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

    def test_submission_journal_committed_before_and_after_remote_submit(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(video, "VOLUME_ROOT", directory), \
             patch.object(video, "SGLangVideoClient") as client:
            vol = FakeVolume()
            journal = Path(directory) / self.prefix / "001.progress.json"
            observations = []

            def submit(_payload, **_kwargs):
                observations.append((vol.commits, json.loads(journal.read_text())["status"]))
                return "video-123"

            def wait(*_args, **_kwargs):
                entry = json.loads(journal.read_text())
                observations.append((vol.commits, entry["status"], entry["video_id"]))

            client.return_value.submit.side_effect = submit
            client.return_value.wait.side_effect = wait
            client.return_value.content.return_value = MP4
            self.assertEqual(self.call(vol)["completed"], 1)
            self.assertEqual(observations, [(1, "submitting"), (2, "running", "video-123")])
            self.assertFalse(journal.exists())

    def test_submission_timeout_does_not_resubmit_unknown_remote_job(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(video, "VOLUME_ROOT", directory), \
             patch.object(video, "SGLangVideoClient") as client:
            client.return_value.submit.side_effect = TimeoutError("response lost")
            vol = FakeVolume()
            self.assertEqual(self.call(vol)["failed"], 1)
            journal = Path(directory) / self.prefix / "001.progress.json"
            self.assertEqual(json.loads(journal.read_text())["status"], "submitting")
            self.assertGreaterEqual(vol.commits, 1)
            with self.assertRaises(RuntimeError):
                self.call(vol)
            client.return_value.submit.assert_called_once()

    def test_resume_ignores_stale_part_without_resubmitting(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(video, "VOLUME_ROOT", directory), \
             patch.object(video, "SGLangVideoClient") as client:
            dest = Path(directory) / self.prefix
            dest.mkdir(parents=True)
            stale = dest / "001.mp4.part"
            stale.write_bytes(b"incomplete previous download")
            progress = dest / "001.progress.json"
            signature = video.execution_hash(self.items[0], "manifest-hash")
            progress.write_text(json.dumps({
                "status": "running", "intent_hash": self.items[0]["intent_hash"],
                "execution_hash": signature, "video_id": "video-existing",
            }))
            client.return_value.content.return_value = MP4
            report = self.call(FakeVolume())
            self.assertEqual(report["completed"], 1)
            client.return_value.submit.assert_not_called()
            client.return_value.wait.assert_called_once()
            self.assertEqual(client.return_value.wait.call_args.args[0], "video-existing")
            self.assertEqual((dest / "001.mp4").read_bytes(), MP4)
            self.assertEqual(stale.read_bytes(), b"incomplete previous download")
            self.assertFalse(progress.exists())
            self.assertEqual(list(dest.glob("001.mp4.*.part")), [])

    def test_publish_failure_cleans_new_part_and_preserves_remote_id(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(video, "VOLUME_ROOT", directory), \
             patch.object(video, "SGLangVideoClient") as client:
            dest = Path(directory) / self.prefix
            dest.mkdir(parents=True)
            client.return_value.submit.return_value = "video-existing"
            client.return_value.content.return_value = MP4
            original_replace = Path.replace

            def fail_publish(path, target):
                if path.name.startswith("001.mp4.") and path.suffix == ".part":
                    raise OSError("simulated publication failure")
                return original_replace(path, target)

            with patch.object(Path, "replace", autospec=True, side_effect=fail_publish):
                report = self.call(FakeVolume())
            self.assertEqual(report["failed"], 1)
            self.assertEqual(list(dest.glob("001.mp4.*.part")), [])
            self.assertFalse((dest / "001.mp4").exists())
            progress = json.loads((dest / "001.progress.json").read_text())
            self.assertEqual(progress["video_id"], "video-existing")
            client.return_value.submit.assert_called_once()
            self.assertEqual(self.call(FakeVolume())["completed"], 1)
            client.return_value.submit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
