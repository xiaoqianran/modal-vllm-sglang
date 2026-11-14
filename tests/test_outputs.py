"""Unified project/scene output layout and recorded content identity."""
from pathlib import Path
import tempfile
import unittest

from shared import outputs
from shared.files import read_json, sha256_file


class OutputTests(unittest.TestCase):
    def test_scene_layout_and_artifact_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            first = outputs.run_dir("006-demo", root=directory)
            second = outputs.run_dir("006-demo", root=directory)
            self.assertEqual(first, second)
            self.assertEqual(first.relative_to(directory).as_posix(), "006-demo/001-main")
            target = first / "001.png"
            target.write_bytes(b"image")
            artifact = outputs.record_artifact(first, target, "image")
            self.assertEqual(artifact["path"], "001.png")
            self.assertEqual(artifact["sha256"], sha256_file(target))
            self.assertEqual(read_json(first / ".runner/items/001/image/artifacts.json")["artifacts"]["image"], artifact)
            for invalid in ("..", "a/b", "CON"):
                with self.assertRaises(ValueError):
                    outputs.run_dir(invalid, root=directory)
