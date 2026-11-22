import shutil
import tempfile
import unittest
from pathlib import Path

from pipeline import s01_build_plan as runner
from runtime.utils.files import read_json, atomic_json


class FrameDimensionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(runner.ROOT / "workflows", self.root / "workflows")
        self.task = self.root / "tasks/004-pika-frames-demo.json"
        atomic_json(self.task, read_json(runner.ROOT / "tasks/004-pika-frames-demo.json"))

    def test_004_generates_all_keyframes_on_the_observed_video_canvas(self):
        plan = runner.build_plan(self.task, root=self.root)
        frames = [j for j in plan["jobs"] if j["step"] in {"first", "last"}]
        self.assertEqual(len(frames), 12)
        self.assertEqual({j["params"]["size"] for j in frames}, {"864x480"})
        self.assertTrue(all(j["params"]["target"]["short_edge"] == 480
                            for j in plan["jobs"] if j["step"] == "video"))

    def test_square_keyframes_are_rejected_before_registration(self):
        path = self.root / "workflows/storyboard-to-video-frames.json"
        workflow = read_json(path)
        workflow["steps"][0]["params"]["size"] = "1024x1024"
        atomic_json(path, workflow)
        with self.assertRaisesRegex(ValueError, "does not match video"):
            runner.build_plan(self.task, root=self.root)
        self.assertFalse((self.root / ".runtime").exists())

    def test_first_and_last_must_use_identical_dimensions(self):
        path = self.root / "workflows/storyboard-to-video-frames.json"
        workflow = read_json(path)
        workflow["steps"][1]["params"]["size"] = "832x480"
        atomic_json(path, workflow)
        with self.assertRaisesRegex(ValueError, "must be identical"):
            runner.build_plan(self.task, root=self.root)
