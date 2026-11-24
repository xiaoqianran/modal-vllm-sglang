"""Offline regression guards for source-imported Modal functions and classes."""
import ast
import importlib
import tempfile
import unittest
from pathlib import Path

from runtime.serving.model_runtime import create_app
from runtime.serving import model_runtime, snapshot_classes
from runtime.utils.files import atomic_json, read_json

ROOT = Path(__file__).resolve().parents[1]


class SourceImportContractTests(unittest.TestCase):
    def test_no_serialized_remote_functions_in_runtime_or_cli(self):
        for folder in ("runtime", "orchestration", "tools", "models"):
            for path in (ROOT / folder).rglob("*.py"):
                if any(part.startswith(".") or part == "__pycache__"
                       for part in path.relative_to(ROOT).parts):
                    continue
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call):
                        for kw in node.keywords:
                            if kw.arg == "serialized":
                                self.assertIsInstance(kw.value, ast.Constant, str(path))
                                self.assertIs(kw.value.value, False, str(path))

    def test_named_runtime_classes_are_importable_in_both_snapshot_modes(self):
        for name, cls in model_runtime._RUNTIME_CLASSES.items():
            self.assertEqual(cls.__name__, name)
            self.assertIs(getattr(importlib.import_module(cls.__module__), name), cls)
            snapshot = snapshot_classes.RUNTIME_CLASSES[name]
            self.assertEqual(snapshot.__name__, name)
            self.assertIs(getattr(importlib.import_module(snapshot.__module__), name), snapshot)
            self.assertNotIn("capture_engine", dir(cls))
            self.assertIn("capture_engine", dir(snapshot))
            self.assertIn("serve", dir(cls))
            self.assertIn("serve", dir(snapshot))

    def test_real_media_specs_keep_deployed_class_name_and_independent_ports(self):
        for folder in ("001-moss-image", "002-pika-h3-flex", "005-minimax-h3-image"):
            spec = read_json(ROOT / "models" / folder / "model.json")
            for mode in ("off", "gpu"):
                with self.subTest(folder=folder, mode=mode), tempfile.TemporaryDirectory() as tmp:
                    config = Path(tmp) / "model.json"
                    atomic_json(config, {**spec, "memory_snapshot": mode})
                    app, handle = create_app(config)
                    self.assertIs(app.registered_classes[spec["modal_class"]], handle)
                    self.assertIn("provision_info", app.registered_functions)
                    classes = (snapshot_classes.RUNTIME_CLASSES if mode == "gpu"
                               else model_runtime._RUNTIME_CLASSES)
                    # Registration must not permanently overwrite source metadata.
                    self.assertNotIn("serve", classes[spec["modal_class"]].__dict__)


if __name__ == "__main__":
    unittest.main()
