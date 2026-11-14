"""CPU-only MOSS shared-runtime regression: reuse existing vLLM and output hashes."""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "001-moss-image"))
spec = importlib.util.spec_from_file_location(
    "moss_batch_unified", ROOT / "001-moss-image" / "image.py")
batch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(batch)


class Volume:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


class MossUnifiedTests(unittest.TestCase):
    def test_image_generated_once_and_next_call_skips_without_loading_new_model(self):
        png = b"\x89PNG\r\n\x1a\n" + b"x" * 11000
        shots = [{"id": "001-scene__001", "prompt": "rain on glass"}]
        with tempfile.TemporaryDirectory() as directory:
            volume = Volume()
            with patch.object(batch, "OUTPUT_ROOT", Path(directory)), \
                 patch.object(batch, "VLLMImageClient") as client:
                client.return_value.generate.return_value = (png, ".png")
                events = list(batch.stream_batch(shots, "008-moss-storyboard/test", volume, 12.5))
                self.assertEqual([e["status"] for e in events], ["ready", "ok", "finished"])
                self.assertEqual(events[0]["startup_seconds"], 12.5)
                self.assertEqual(volume.commits, 1)
                self.assertEqual(client.return_value.generate.call_count, 1)
                metadata = Path(directory) / "008-moss-storyboard/test/001-scene__001.json"
                self.assertTrue(metadata.is_file())

                again = list(batch.stream_batch(shots, "008-moss-storyboard/test", volume, 12.5))
                self.assertEqual([e["status"] for e in again], ["ready", "skipped", "finished"])
                self.assertEqual(client.return_value.generate.call_count, 1)

                shots[0]["prompt"] = "changed scene"
                with self.assertRaises(FileExistsError):
                    list(batch.stream_batch(shots, "008-moss-storyboard/test", volume, 12.5))


if __name__ == "__main__":
    unittest.main()
