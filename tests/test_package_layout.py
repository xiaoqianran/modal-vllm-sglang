"""验证公共子包重组不会破坏模型入口与 Modal 容器挂载。"""
import importlib
import unittest
from pathlib import Path

from runtime.config.model_spec import discover
from runtime.serving import modal_setup
from runtime.serving.model_runtime import create_app
from runtime.tasks import task_spec


class PackageLayoutTests(unittest.TestCase):
    def test_all_public_modules_are_importable(self):
        groups = {
            "config": ("model_spec", "model_cache", "cache_inventory", "cache_publication"),
            "serving": ("legacy_cache_preparer", "modal_setup", "process_lifecycle",
                        "inference_api", "inference_gateway"),
            "telemetry": ("schema", "recorder", "sampler", "storage", "collector", "report"),
            "tasks": ("task_spec", "job_client", "task_lifecycle", "task_identity",
                      "result_integrity", "result_summary", "batch"),
            "utils": ("files", "outputs", "http", "media"),
        }
        for group, modules in groups.items():
            for name in modules:
                with self.subTest(module=f"runtime.{group}.{name}"):
                    importlib.import_module(f"runtime.{group}.{name}")

    def test_modal_image_mounts_entire_shared_package(self):
        shared_dir = Path(__file__).resolve().parents[1] / "runtime"
        self.assertEqual(modal_setup.ROOT, shared_dir)
        self.assertEqual(task_spec.ROOT, shared_dir.parent)

    def test_model_discovery_remains_unchanged(self):
        self.assertEqual(set(discover()), {
            "moss.t2i", "pika.t2va", "pika.fl2va", "minimax.t2i", "minimax.i2i", "minimax-omni.t2i", "minimax-omni.i2i"
        })

    def test_model_owned_validators_are_importable(self):
        for name in ("models.001-moss-image.model_impl.params",
                     "models.002-pika-h3-flex.model_impl.params",
                     "models.005-minimax-h3-image-sglang.model_impl.params"):
            with self.subTest(name=name):
                self.assertTrue(callable(importlib.import_module(name).validate))

    def test_obsolete_public_names_are_not_canonical_modules(self):
        root = Path(__file__).resolve().parents[1]
        for relative in (
            "runtime/config/azure_cache.py",
            "runtime/config/cache.py",
            "runtime/serving/model_service.py",
            "runtime/serving/runtime.py",
            "orchestration/adapters.py",
        ):
            with self.subTest(file=relative):
                self.assertFalse((root / relative).exists())
