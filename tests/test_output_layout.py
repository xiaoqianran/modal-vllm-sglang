from tests.scheduler_helpers import drain
import shutil
import tempfile
import unittest
from pathlib import Path

from pipeline import s01_build_plan as runner
from orchestration import sdk, state
from orchestration.monitor import render
from runtime.telemetry.recorder import close_local
from runtime.utils.files import read_json
from tests.test_sdk_orchestration import FakeBackend


class OutputLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(close_local, self.root / ".runtime/logs")
        shutil.copytree(runner.ROOT / "workflows", self.root / "workflows")
        task = self.root / "tasks/006-layout.json"
        state.save(task, {"version": 1, "name": "006-layout", "workflow": "storyboard-to-video",
                          "items": [{"id": "001", "first_prompt": "lake", "last_prompt": "shore", "motion_prompt": "move"}]})
        self.plan = runner.build_plan(task, root=self.root)

    def test_flat_media_and_internal_state_resume_without_duplicate_calls(self):
        directory = runner.new_run(self.plan, root=self.root)
        self.assertEqual(directory, self.root / ".runtime/tasks/006-layout/001-main")
        backend = FakeBackend()
        backend.videos_complete = True
        drain(directory, backend=backend, timeout=3, poll=0.001)
        media = self.root / "outputs/006-layout"
        self.assertEqual({p.name for p in media.iterdir()}, {"001_first.png", "001_last.png", "001_video.mp4"})
        self.assertEqual(runner.new_run(self.plan, root=self.root), directory)
        drain(directory, backend=backend, timeout=3, poll=0.001)
        self.assertEqual(len(backend.submissions), 3)
        self.assertEqual(state.resolve_task(self.root, "outputs/006-layout"), directory)
        render(self.root / "outputs")
        html = (self.root / "outputs/监控006-layout.html").read_text(encoding="utf-8")
        self.assertIn("006-layout/001_video.mp4", html)
        self.assertIn("<video controls", html)
        self.assertTrue((directory / "task.json").exists())
        self.assertTrue(list((self.root / ".runtime/logs/tasks").glob("*.jsonl")))
        second = self.root / "tasks/006-other.json"
        task = read_json(self.root / "tasks/006-layout.json")
        task["name"] = "006-other"
        state.save(second, task)
        runner.new_run(runner.build_plan(second, root=self.root), root=self.root)
        self.assertTrue((self.root / "outputs/监控006-other.html").exists())
        self.assertNotIn("<h2>006-other</h2>", (self.root / "outputs/监控006-layout.html").read_text(encoding="utf-8"))
        self.assertFalse((self.root / "outputs/监控.html").exists())
