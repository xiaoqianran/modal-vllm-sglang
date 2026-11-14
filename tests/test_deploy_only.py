"""Deployment contracts for all model configurations and common GPU lifecycle."""
import ast
import runpy
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ("001-moss-image", "002-pika-h3-flex", "005-minimax-h3-image")


class DeployOnlyTests(unittest.TestCase):
    def test_all_models_register_the_same_template(self):
        from shared.model_spec import validate
        from shared.files import read_json
        for folder in MODELS:
            with self.subTest(folder=folder):
                spec = validate(read_json(ROOT / folder / "model.json"))
                app = runpy.run_path(str(ROOT / folder / "app.py"))
                self.assertIn("Model", app)
                self.assertEqual(app["app"].name, spec["app_name"])
                download = runpy.run_path(str(ROOT / folder / "download_model.py"))
                self.assertIn("download", download)
                self.assertIn("check_cache", download)
                for name in ("image.py", "video.py", "model_cache.py", "proxy.py"):
                    self.assertFalse((ROOT / folder / name).exists())

    def test_common_service_has_one_gpu_class_and_private_http(self):
        tree = ast.parse((ROOT / "shared/model_service.py").read_text(encoding="utf-8"))
        classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
        self.assertEqual(len(classes), 1)
        cls = classes[0]
        self.assertEqual(cls.name, "Model")
        methods = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
        self.assertTrue({"start", "stop", "serve", "generate_batch_stream"} <= methods)
        decorator = next(d for d in cls.decorator_list if isinstance(d, ast.Call)
                         and isinstance(d.func, ast.Attribute) and d.func.attr == "cls")
        self.assertTrue(any(k.arg == "max_containers" and isinstance(k.value, ast.Constant)
                            and k.value.value == 1 for k in decorator.keywords))
        start = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "start")
        self.assertFalse(any(isinstance(n, ast.Name) and n.id == "prepare_cache" for n in ast.walk(start)))

    def test_no_temporary_gpu_app_entrypoints(self):
        for folder in MODELS:
            for name in ("app.py", "run.py"):
                source = (ROOT / folder / name).read_text()
                self.assertNotIn("app.run(", source)
                self.assertNotIn("@app.local_entrypoint", source)
