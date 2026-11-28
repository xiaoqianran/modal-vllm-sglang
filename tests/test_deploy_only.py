"""Deployment contracts for all model configurations and common GPU lifecycle."""
import ast
import runpy
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ("models/001-moss-image", "models/002-pika-h3-flex", "models/005-minimax-h3-image-sglang")


class DeployOnlyTests(unittest.TestCase):
    def test_all_models_register_the_same_template(self):
        from runtime.config.model_spec import validate
        from runtime.utils.files import read_json
        for folder in MODELS:
            with self.subTest(folder=folder):
                spec = validate(read_json(ROOT / folder / "model.json"))
                app = runpy.run_path(str(ROOT / folder / "app.py"))
                self.assertIn("Model", app)
                self.assertEqual(app["app"].name, spec["app_name"])
                download = runpy.run_path(str(ROOT / folder / "download_model.py"))
                self.assertIn("download_main", download)
                self.assertIn("main", download)
                for name in ("image.py", "video.py", "model_cache.py", "proxy.py"):
                    self.assertFalse((ROOT / folder / name).exists())

    def test_common_service_has_one_gpu_class_and_private_http(self):
        # GPU lifecycle and CPU preparation have separate entry modules.
        tree = ast.parse((ROOT / "runtime/serving/model_runtime.py").read_text(encoding="utf-8"))
        classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
        cls = next(n for n in classes if n.name == "ModelRuntime")
        methods = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
        self.assertTrue({"start", "stop", "generate_one", "deployment_info"} <= methods)
        # Named variants share one GPU lifecycle rather than duplicate it.
        for variant in classes:
            if variant.name not in {"ModelRuntime", "SnapshotModelRuntime"}:
                self.assertTrue(all(isinstance(n, ast.Pass) for n in variant.body))
        self.assertTrue(any(isinstance(n, ast.Assign) and
                            any(isinstance(t, ast.Name) and t.id == "serve" for t in n.targets)
                            for n in cls.body))
        facade = ast.parse((ROOT / "runtime/serving/legacy_cache_preparer.py").read_text(encoding="utf-8"))
        self.assertFalse(any(isinstance(n, ast.ClassDef) for n in ast.walk(facade)))
        registrations = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Attribute) and n.func.attr == "cls"]
        self.assertEqual(len(registrations), 1)
        keywords = {k.arg: k.value for k in registrations[0].keywords}
        self.assertEqual(ast.unparse(keywords["max_containers"]), "spec.get('max_containers', 1)")
        self.assertEqual(ast.unparse(keywords["buffer_containers"]), "spec.get('buffer_containers', 0)")
        self.assertEqual(keywords["min_containers"].value, 0)
        concurrent_calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                            and isinstance(n.func, ast.Attribute) and n.func.attr == "concurrent"]
        self.assertEqual(len(concurrent_calls), 1)
        concurrency = {k.arg: ast.unparse(k.value) for k in concurrent_calls[0].keywords}
        self.assertEqual(concurrency["max_inputs"], "spec.get('max_inputs', 1)")
        self.assertEqual(concurrency["target_inputs"], "spec.get('target_inputs')")
        self.assertIs(keywords["serialized"].value, False)
        for method in (n for n in ast.walk(cls) if isinstance(n, ast.FunctionDef)):
            self.assertFalse(any(isinstance(n, ast.Name) and n.id == "prepare_cache" for n in ast.walk(method)))
        web_registrations = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                             and isinstance(n.func, ast.Attribute) and n.func.attr == "web_server"]
        self.assertTrue(web_registrations)
        for call in web_registrations:
            self.assertTrue(next(k.value.value for k in call.keywords if k.arg == "requires_proxy_auth"))

    def test_only_minimax_omni_enables_two_containers(self):
        from runtime.config.model_spec import validate
        from runtime.utils.files import read_json
        for path in (ROOT / "models").glob("*/model.json"):
            with self.subTest(model=path.parent.name):
                spec = validate(read_json(path))
                omni = spec["service"] == "minimax-omni"
                self.assertEqual(spec.get("max_containers", 1), 2 if omni else 1)
                if omni:
                    self.assertEqual(spec["max_inputs"], 2)
                    self.assertEqual(spec["target_inputs"], 1)
                    self.assertEqual(spec["buffer_containers"], 0)
                else:
                    self.assertNotIn("target_inputs", spec)
                    self.assertEqual(spec.get("buffer_containers", 0), 0)

    def test_no_temporary_gpu_app_entrypoints(self):
        for folder in MODELS:
            self.assertFalse((ROOT / folder / "run.py").exists())
            for name in ("app.py",):
                source = (ROOT / folder / name).read_text()
                self.assertNotIn("app.run(", source)
                self.assertNotIn("@app.local_entrypoint", source)
