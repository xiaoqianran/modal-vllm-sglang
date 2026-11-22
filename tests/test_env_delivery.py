"""Environment-selected delivery defaults and frozen legacy tasks."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline import s01_build_plan
from orchestration import state
from runtime.delivery.config import configured_delivery
from runtime.utils.files import read_json


class EnvironmentDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(s01_build_plan.ROOT / "workflows", self.root / "workflows")
        (self.root / "tasks").mkdir()
        self.task_path = self.root / "tasks" / "006-env-demo.json"
        state.save(self.task_path, {
            "version": 1, "name": "006-env-demo", "workflow": "moss-t2i",
            "items": [{"id": "001", "prompt": "lake"}]
        })

    def enable_hf(self):
        (self.root / ".env").write_text(
            "OUTPUT_DELIVERY_PROVIDER=huggingface\n"
            "HF_ASSET_BUCKET=seachen/modal-vllm-sglang-assets\n",
            encoding="utf-8"
        )

    def test_new_plan_uses_hf_from_env(self):
        self.enable_hf()
        with patch.dict(os.environ, {"OUTPUT_DELIVERY_PROVIDER": "", "HF_ASSET_BUCKET": ""}):
            plan = s01_build_plan.build_plan(self.task_path, root=self.root)
        self.assertEqual(plan["delivery"]["provider"], "huggingface")
        self.assertEqual(plan["delivery"]["bucket"], "seachen/modal-vllm-sglang-assets")
        directory = s01_build_plan.new_run(plan, root=self.root)
        self.assertEqual(read_json(directory / "plan.json")["delivery"], plan["delivery"])

    def test_legacy_registered_task_keeps_local_after_default_change(self):
        with patch.dict(os.environ, {"OUTPUT_DELIVERY_PROVIDER": "", "HF_ASSET_BUCKET": ""}):
            plan = s01_build_plan.build_plan(self.task_path, root=self.root)
            self.assertNotIn("delivery", plan)
            original = s01_build_plan.new_run(plan, root=self.root)
            self.enable_hf()
            updated = s01_build_plan.build_plan(self.task_path, root=self.root)
            self.assertEqual(updated["delivery"]["provider"], "huggingface")
            self.assertEqual(s01_build_plan.new_run(updated, root=self.root), original)
            frozen = read_json(original / "plan.json")
            self.assertNotIn("delivery", frozen)
            self.assertEqual(frozen["hash"], plan["hash"])

    def test_explicit_local_overrides_environment(self):
        self.enable_hf()
        task = read_json(self.task_path)
        task["delivery"] = {"provider": "local"}
        state.save(self.task_path, task)
        plan = s01_build_plan.build_plan(self.task_path, root=self.root)
        self.assertEqual(plan["delivery"], {"provider": "local"})

    def test_environment_override(self):
        self.enable_hf()
        with patch.dict(os.environ, {"OUTPUT_DELIVERY_PROVIDER": "local"}):
            self.assertEqual(configured_delivery(self.root), {"provider": "local"})

    def test_hf_registration_checks_cloud_cpu_before_registering(self):
        import io
        from run import main
        self.enable_hf()
        plan = s01_build_plan.build_plan(self.task_path, root=self.root)
        with patch("sys.argv", ["run.py", "run", str(self.task_path)]), \
                patch("run.build_plan", return_value=plan), \
                patch("run.new_run") as new_run, \
                patch("modal.Function.from_name") as function, \
                patch("sys.stderr", new_callable=io.StringIO):
            function.return_value.remote.side_effect = RuntimeError("CPU archiver unavailable")
            with self.assertRaises(SystemExit) as error:
                main()
            self.assertEqual(error.exception.code, 2)
            new_run.assert_not_called()
            function.assert_called_once_with("modal-vllm-sglang-assets", "check_bucket")


if __name__ == "__main__":
    unittest.main()
