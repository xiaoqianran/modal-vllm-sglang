"""Offline validation of supported SDK workflow plans."""
import copy
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from pipeline import s01_build_plan as runner
from orchestration import state
from runtime.utils.files import read_json


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(runner.ROOT / "workflows", self.root / "workflows")
        shutil.copytree(runner.ROOT / "tasks", self.root / "tasks")
        self.task_path = self.root / "tasks/001-story-demo.json"

    def plan(self):
        return runner.build_plan(self.task_path, root=self.root)

    def test_seed_stable_on_reordering_and_defaults_frozen(self):
        task = read_json(self.task_path)
        task["items"].append(dict(task["items"][0], id="002"))
        state.save(self.task_path, task)
        before = self.plan()
        task["items"].reverse()
        state.save(self.task_path, task)
        after = self.plan()
        self.assertEqual({j["key"]: j["inputs"]["seed"] for j in before["jobs"]},
                         {j["key"]: j["inputs"]["seed"] for j in after["jobs"]})
        self.assertEqual(before["jobs"][0]["params"]["guidance_scale"], 3.0)

    def test_offline_plan_and_validation_before_submission(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(len(self.plan()["jobs"]), 3)
        workflow_path = self.root / "workflows/storyboard-to-video.json"
        original = read_json(workflow_path)
        bad_graphs = []
        graph = copy.deepcopy(original)
        graph["steps"][0]["inputs"]["prompt"] = {"ref": "video.video"}
        bad_graphs.append(graph)
        graph = copy.deepcopy(original)
        graph["steps"][2]["params"] = {"num_inference_steps": 7}
        bad_graphs.append(graph)
        graph = copy.deepcopy(original)
        graph["steps"][0]["params"] = {"typo": 1}
        bad_graphs.append(graph)
        graph = copy.deepcopy(original)
        graph["steps"][2]["inputs"]["first_frame"] = {"ref": "missing.image"}
        bad_graphs.append(graph)
        for graph in bad_graphs:
            state.save(workflow_path, graph)
            with self.assertRaises(ValueError):
                self.plan()
        self.assertFalse((self.root / "outputs").exists())

    def test_duplicate_items_and_missing_input_rejected(self):
        task = read_json(self.task_path)
        task["items"].append(task["items"][0])
        state.save(self.task_path, task)
        with self.assertRaises(ValueError):
            self.plan()
        task["items"] = [{"id": "001"}]
        state.save(self.task_path, task)
        with self.assertRaises(ValueError):
            self.plan()

    def test_path_escape_rejected(self):
        with self.assertRaises(ValueError):
            state.local_path(self.root, "../escape")
        for value in ("..", "CON", "nul", "a/b"):
            with self.assertRaises(ValueError):
                runner.identifier(value)
