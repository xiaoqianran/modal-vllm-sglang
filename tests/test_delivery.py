"""Output delivery policies and recovery; no remote calls."""
import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestration import sdk, state
from pipeline import s01_build_plan
from runtime.delivery import normalize_delivery
from runtime.utils.files import read_json
from tests.scheduler_helpers import drain
from tests.test_sdk_orchestration import FakeBackend, PNG


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        shutil.copytree(s01_build_plan.ROOT / "workflows", self.root / "workflows")
        (self.root / "tasks").mkdir()
        self.backend = FakeBackend()
        self.backend.videos_complete = True

    def create(self, delivery=None):
        task = {"version": 1, "name": "006-hf-demo",
                "workflow": "storyboard-to-video",
                "items": [{"id": "001", "first_prompt": "lake",
                           "last_prompt": "shore", "motion_prompt": "drift"}]}
        if delivery is not None:
            task["delivery"] = delivery
        path = self.root / "tasks/006-hf-demo.json"
        state.save(path, task)
        plan = s01_build_plan.build_plan(path, root=self.root)
        return s01_build_plan.new_run(plan, root=self.root), plan

    @staticmethod
    def config(provider):
        return {"provider": provider, "bucket": "team/public",
                "verify": "sha256", "compression": "adaptive_lossless"}

    def test_policy_validation_and_frozen_plan(self):
        self.assertEqual(normalize_delivery(None), {"provider": "local"})
        for value in ({}, {"provider": "local", "bucket": "a/b"},
                      {"provider": "missing"},
                      {"provider": "both", "bucket": "../bad"},
                      {"provider": "both", "bucket": "a/b", "min_saving": True}):
            if value == {}:
                self.assertEqual(normalize_delivery(value), {"provider": "local"})
            else:
                with self.subTest(value=value), self.assertRaises(ValueError):
                    normalize_delivery(value)
        self.assertEqual(normalize_delivery({"provider": "huggingface"})["bucket"],
                         "seachen/modal-vllm-sglang-assets")
        self.assertEqual(normalize_delivery({"provider": "both"})["bucket"],
                         "seachen/modal-vllm-sglang-assets")
        directory, plan = self.create(self.config("huggingface"))
        self.assertEqual(plan["delivery"]["provider"], "huggingface")
        saved = read_json(directory / "plan.json")
        self.assertEqual(saved["delivery"], plan["delivery"])
        self.assertEqual(sdk.snapshot(directory)[0]["hash"], plan["hash"])

    def test_hf_only_no_outputs_or_gpu_resubmission(self):
        directory, plan = self.create(self.config("huggingface"))
        calls = []
        def archive(directory, job, record, plan):
            calls.append(job["key"])
            name = job["key"].replace("/", "-")
            suffix = ".mp4" if job["step"] == "video" else ".webp"
            remote_path = "projects/006-hf-demo/images/" + name + suffix
            return {"provider": "huggingface_bucket", "bucket": "team/public",
                    "path": remote_path, "url": "https://example.test/" + name,
                    "kind": "video" if job["step"] == "video" else "image",
                    "stored_bytes": 42, "status": "archived"}
        self.backend.archive = archive
        result = drain(directory, backend=self.backend)
        saved = sdk.snapshot(directory)[1]
        self.assertEqual(result["cloud_archived"], 3)
        self.assertEqual(result["local_downloaded"], 0)
        self.assertTrue(saved.get("finished_at"))
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(self.backend.submissions), 3)
        self.assertEqual(list((self.root / "outputs/006-hf-demo").iterdir()), [])
        from orchestration.monitor import render
        render(self.root / "outputs")
        html = (self.root / "outputs/监控006-hf-demo.html").read_text(encoding="utf-8")
        self.assertIn("https://huggingface.co/buckets/team/public/resolve/projects/", html)
        self.assertIn("<video controls", html)
        sdk.request_resume(directory)
        drain(directory, backend=self.backend)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(self.backend.submissions), 3)

    def test_both_retries_archive_without_regeneration(self):
        directory, plan = self.create(self.config("both"))
        failed = [True]
        attempts = []
        def archive(directory, job, record, plan):
            attempts.append(job["key"])
            if failed[0]:
                raise OSError("HF unavailable")
            return {"provider": "huggingface_bucket", "status": "archived",
                    "url": "https://example.test/" + job["key"],
                    "kind": "image", "stored_bytes": 10}
        self.backend.archive = archive
        first = drain(directory, backend=self.backend)
        self.assertEqual(first["archive_errors"], 3)
        self.assertEqual(first["local_downloaded"], 3)
        self.assertEqual(len(self.backend.submissions), 3)
        self.assertFalse(sdk.snapshot(directory)[1].get("finished_at"))
        failed[0] = False
        sdk.request_resume(directory)
        next_result = drain(directory, backend=self.backend)
        self.assertEqual(next_result["cloud_archived"], 3)
        self.assertEqual(next_result["archive_errors"], 0)
        self.assertEqual(next_result["local_downloaded"], 3)
        self.assertTrue(sdk.snapshot(directory)[1].get("finished_at"))
        self.assertEqual(len(self.backend.submissions), 3)
        self.assertEqual(len(attempts), 6)

if __name__ == "__main__":
    unittest.main()
