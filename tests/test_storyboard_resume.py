"""CPU-only resume and integrity tests for the MOSS single-batch runner."""
import hashlib
import importlib.util
from importlib.machinery import ModuleSpec
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import nullcontext
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "moss_resume_under_test", ROOT / "scripts" / "generate_again_love_v3.py"
)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class FakeVolume:
    def __init__(self, files):
        self.files = files

    def read_file(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        blob = self.files[path]
        return (blob[i:i + 1000] for i in range(0, len(blob), 1000))


class StoryboardResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.base = self.root / "outputs" / "my-drama"
        self.task_path = self.root / "test.json"
        self.task = {"project": "my-drama", "scene": "001-first",
                     "items": [{"id": "001", "prompt": "first"},
                               {"id": "002", "prompt": "second"}]}
        self.task_path.write_text(json.dumps(self.task), encoding="utf-8")
        self.shots, self.prefix = runner.load_plan("my-drama", [self.task_path])
        self.data = b"\x89PNG\r\n\x1a\n" + b"a" * 11000

    def test_stable_prefix_for_unchanged_task(self):
        _, again = runner.load_plan("my-drama", [self.task_path])
        self.assertEqual(self.prefix, again)
        self.task["items"][1]["prompt"] = "changed"
        self.task_path.write_text(json.dumps(self.task), encoding="utf-8")
        _, changed = runner.load_plan("my-drama", [self.task_path])
        self.assertNotEqual(self.prefix, changed)

    def test_resume_skips_first_download_and_only_selects_second(self):
        event = {"file": self.shots[0]["id"] + ".png",
                 "sha256": hashlib.sha256(self.data).hexdigest()}
        volume = FakeVolume({f"/{self.prefix}/{event['file']}": self.data})
        self.assertTrue(runner.receive_image(volume, self.prefix, self.base, self.shots[0], event))
        self.assertEqual([x["id"] for x in runner.select_pending(self.base, self.shots)],
                         ["001-first__002"])
        self.assertFalse(runner.receive_image(volume, self.prefix, self.base, self.shots[0], event))

    def test_recover_legacy_volume_without_gpu(self):
        shot = self.shots[0]
        meta = {"status": "completed", "file": shot["id"] + ".png",
                "sha256": hashlib.sha256(self.data).hexdigest()}
        legacy = "008-moss-storyboard/old-batch/20260101"
        volume = FakeVolume({f"/{legacy}/{shot['id']}.json": json.dumps(meta).encode(),
                             f"/{legacy}/{meta['file']}": self.data})
        self.assertEqual(runner.recover_legacy(volume, legacy, self.base, self.shots), 1)
        self.assertEqual(len(runner.select_pending(self.base, self.shots)), 1)

    def test_rejects_corrupt_or_modified_existing_file(self):
        shot = self.shots[0]
        event = {"file": shot["id"] + ".png", "sha256": hashlib.sha256(self.data).hexdigest()}
        volume = FakeVolume({f"/{self.prefix}/{event['file']}": self.data})
        runner.receive_image(volume, self.prefix, self.base, shot, event)
        path = runner.local_target(self.base, shot)
        path.write_bytes(b"corrupted")
        with self.assertRaises(ValueError):
            runner.select_pending(self.base, self.shots)

    def test_rejects_changed_prompt_against_saved_manifest(self):
        shot = self.shots[0]
        event = {"file": shot["id"] + ".png", "sha256": hashlib.sha256(self.data).hexdigest()}
        volume = FakeVolume({f"/{self.prefix}/{event['file']}": self.data})
        runner.receive_image(volume, self.prefix, self.base, shot, event)
        changed = dict(shot, prompt="a different prompt")
        with self.assertRaises(ValueError):
            runner.select_pending(self.base, [changed])

    def test_lock_blocks_a_second_local_batch(self):
        with runner.single_writer(self.base):
            with self.assertRaises(RuntimeError):
                with runner.single_writer(self.base):
                    pass

    def test_end_to_end_interruption_then_resume_without_regenerating_first(self):
        """Mock Modal streams image 1, loses connection, then resumes image 2."""
        data2 = b"\x89PNG\r\n\x1a\n" + b"b" * 11000
        remote_data = {
            f"/{self.prefix}/{shot['id']}.png": data
            for shot, data in zip(self.shots, (self.data, data2))
        }
        volume = FakeVolume(remote_data)
        calls = []

        class FakeStream:
            def remote_gen(self, pending, prefix):
                calls.append([x["id"] for x in pending])
                yield {"status": "ready", "total": len(pending), "gpu": "fake"}
                for record in pending:
                    data = self_data[record["id"]]
                    yield {"status": "ok", "id": record["id"],
                           "file": record["id"] + ".png",
                           "sha256": hashlib.sha256(data).hexdigest()}
                    if len(calls) == 1:
                        raise ConnectionError("simulated client disconnect")
                yield {"status": "finished"}

        self_data = {shot["id"]: data
                     for shot, data in zip(self.shots, (self.data, data2))}

        class FakeLoader:
            def create_module(self, spec):
                return None

            def exec_module(self, module):
                module.volume = volume
                module.GPU = "fake"
                module.app = type("App", (), {"run": lambda self: nullcontext()})()
                module.generate_batch_stream = FakeStream()

        fake_spec = ModuleSpec("fake_moss_backend", FakeLoader())
        with patch.object(runner, "ROOT", self.root), patch.object(
                runner.importlib.util, "spec_from_file_location", return_value=fake_spec):
            with self.assertRaises(ConnectionError):
                runner.run("my-drama", [self.task_path])
            self.assertEqual(len(runner.select_pending(self.base, self.shots)), 1)
            runner.run("my-drama", [self.task_path])
            self.assertEqual(len(runner.select_pending(self.base, self.shots)), 0)
            self.assertEqual(calls, [["001-first__001", "001-first__002"],
                                     ["001-first__002"]])
            status = json.loads((self.base / ".runner" / "moss-v3-progress.json").read_text())
            self.assertEqual(status["status"], "completed")


if __name__ == "__main__":
    unittest.main()
