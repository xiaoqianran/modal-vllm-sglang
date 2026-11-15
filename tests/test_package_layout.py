"""验证公共子包重组不会破坏模型入口与 Modal 容器挂载。"""
import importlib
import unittest
from pathlib import Path

from shared.config.model_spec import discover
from shared.serving import modal_setup
from shared.serving.model_runtime import create_app
from shared.tasks import task_spec


class PackageLayoutTests(unittest.TestCase):
    def test_all_public_modules_are_importable(self):
        groups = {
            "config": ("model_spec", "model_cache", "cache", "params_moss",
                       "params_pika", "params_minimax"),
            "serving": ("model_service", "modal_setup", "runtime",
                        "inference_api", "inference_gateway"),
            "telemetry": ("schema", "recorder", "sampler", "storage", "collector", "report"),
            "tasks": ("task_spec", "job_client", "task_lifecycle", "batch"),
            "utils": ("files", "outputs", "http", "media"),
        }
        for group, modules in groups.items():
            for name in modules:
                with self.subTest(module=f"shared.{group}.{name}"):
                    importlib.import_module(f"shared.{group}.{name}")

    def test_modal_image_mounts_entire_shared_package(self):
        shared_dir = Path(__file__).resolve().parents[1] / "shared"
        self.assertEqual(modal_setup.ROOT, shared_dir)
        self.assertEqual(task_spec.ROOT, shared_dir.parent)

    def test_model_discovery_remains_unchanged(self):
        self.assertEqual(set(discover()), {
            "moss.t2i", "pika.t2va", "pika.fl2va", "minimax.t2i"
        })
