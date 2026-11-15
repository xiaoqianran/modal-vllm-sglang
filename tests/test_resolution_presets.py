import shutil
import tempfile
import unittest
from pathlib import Path

from orchestration import runner, adapters
from shared.config.resolution import catalog, image_size, normalize
from shared.utils.files import atomic_json, read_json


class ResolutionPresetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(runner.ROOT / "workflows", self.root / "workflows")
        self.path = self.root / "tasks/007-resolution-test.json"

    def plan(self, resolution, ratio="16:9", workflow="moss-t2i", **extra):
        task = {"version": 1, "name": self.path.stem, "workflow": workflow,
                "render": {"resolution": resolution, "aspect_ratio": ratio},
                "items": [{"id": "001", "prompt": "lake", "first_prompt": "lake", "last_prompt": "shore", "motion_prompt": "drift"}], **extra}
        atomic_json(self.path, task)
        return runner.build_plan(self.path, root=self.root)

    def test_all_thirty_image_canvases_preserve_ratio_and_alignment(self):
        presets = catalog()
        self.assertEqual(len(presets), 6)
        for tier, ratios in presets.items():
            self.assertEqual(len(ratios), 5)
            for ratio, size in ratios.items():
                width, height = map(int, size.split("x"))
                x, y = map(int, ratio.split(":"))
                self.assertEqual(width * y, height * x)
                self.assertEqual(width % 16, 0)
                self.assertEqual(height % 16, 0)
                self.assertEqual(self.plan(tier, ratio)["jobs"][0]["params"]["size"], size)

    def test_aliases_have_same_execution_identity(self):
        normal = self.plan("768")
        for alias in ("pseudo1k", "pseudo-1k", "伪1K"):
            plan = self.plan(alias)
            self.assertEqual(plan["hash"], normal["hash"])
            self.assertEqual(plan["jobs"][0]["params"]["size"], "768x432")

    def test_video_profile_sets_matching_keyframes_and_rejects_unsupported_tiers(self):
        plan = self.plan("480p", workflow="storyboard-to-video")
        self.assertEqual([j["params"]["size"] for j in plan["jobs"][:2]], ["864x480"] * 2)
        self.assertEqual(plan["jobs"][-1]["params"]["target"]["aspect_ratio"], "16:9")
        for tier in ("256", "512", "1k", "2k", "4k", "768p"):
            with self.assertRaisesRegex(ValueError, "not a declared native canvas"):
                self.plan(tier, workflow="storyboard-to-video")
        self.assertFalse((self.root / ".runtime").exists())

    def test_model_limits_are_not_bypassed_and_conflicting_inputs_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "MiniMax size"):
            adapters.parameters("minimax.t2i", {"size": image_size({"resolution": "512", "aspect_ratio": "16:9"})})
        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            self.plan("512", params={"size": "1024x1024"})
        for invalid in ("8k", "auto", 512, None):
            with self.assertRaises(ValueError):
                normalize({"resolution": invalid, "aspect_ratio": "16:9"})
        self.assertNotEqual(self.plan("512")["hash"], self.plan("1k")["hash"])
