"""分镜生成器：纯渲染、覆盖保护和源数据变更检测。"""
import json
import tempfile
import unittest
from pathlib import Path

from scripts.generate_storyboard import DEFAULT_SOURCE, generate, render, validate_storyboard, video_prompt


class StoryboardGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.story = json.loads(DEFAULT_SOURCE.read_text(encoding="utf-8"))

    def test_generated_documents_and_independent_004_task(self):
        base = DEFAULT_SOURCE.parent.relative_to(DEFAULT_SOURCE.parents[2]).as_posix()
        outputs = render(self.story, story_dir=base, input_dir="inputs/pika")
        for name in ("docs/004-before-rain/image-prompts.json",):
            self.assertEqual(
                json.loads((DEFAULT_SOURCE.parents[2] / name).read_text(encoding="utf-8")),
                json.loads(outputs[name]),
            )
        task = json.loads((DEFAULT_SOURCE.parents[2] / "tasks/004-pika-frames-demo.json").read_text(encoding="utf-8"))
        self.assertEqual(task["workflow"], "storyboard-to-video-frames")
        self.assertEqual([item["id"] for item in task["items"]], [shot["id"] for shot in self.story["shots"]])
        for item, shot in zip(task["items"], self.story["shots"]):
            self.assertEqual(item["motion_prompt"], video_prompt(self.story, shot))
            self.assertTrue(item["first_prompt"] and item["last_prompt"])
        self.assertEqual(len(self.story["shots"]), 6)

    def test_new_directory_generate_then_check_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "docs" / "sample" / "storyboard.json"
            source.parent.mkdir(parents=True)
            source.write_text(json.dumps(self.story), encoding="utf-8")
            paths = generate(source, root=root)
            self.assertEqual(len(paths), 4)
            self.assertTrue(all(path.exists() for path in paths))
            self.assertEqual(generate(source, root=root, check=True), paths)
            self.assertEqual(generate(source, root=root), paths)

            target = root / "docs/sample/分镜.md"
            target.write_text("user edited", encoding="utf-8")
            with self.assertRaises(ValueError):
                generate(source, root=root, check=True)
            with self.assertRaises(FileExistsError):
                generate(source, root=root)
            self.assertEqual(target.read_text(encoding="utf-8"), "user edited")
            generate(source, root=root, force=True)
            self.assertNotEqual(target.read_text(encoding="utf-8"), "user edited")

    def test_invalid_shot_and_unsafe_paths_rejected(self):
        story = json.loads(json.dumps(self.story))
        story["shots"][1]["id"] = story["shots"][0]["id"]
        with self.assertRaisesRegex(ValueError, "unique"):
            validate_storyboard(story)
        story = json.loads(json.dumps(self.story))
        story["input_dir"] = "../../other"
        with self.assertRaisesRegex(ValueError, "input_dir"):
            validate_storyboard(story)
        story = json.loads(json.dumps(self.story))
        story["params"]["target"]["duration_seconds"] = 10
        with self.assertRaisesRegex(ValueError, "duration"):
            validate_storyboard(story)
