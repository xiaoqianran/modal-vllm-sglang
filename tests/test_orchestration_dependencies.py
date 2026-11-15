"""Enforce executor boundaries, including imports inside function bodies."""
import ast
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestration import runner, sdk

ROOT = Path(__file__).resolve().parents[1]


class DependencyTests(unittest.TestCase):
    def test_lower_layers_do_not_import_upper_layers(self):
        allowed = {
            "protocol": set(),
            "sdk": {"adapters", "state", "protocol"},
            "cloud": {"sdk", "state", "protocol"},
            "cloud_client": {"sdk", "state", "protocol"},
        }
        for module, dependencies in allowed.items():
            tree = ast.parse((ROOT / "orchestration" / (module + ".py")).read_text())
            imports = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    name = node.module or ""
                    if node.level or name == "orchestration":
                        imports.update(alias.name.split(".")[0] for alias in node.names)
                    elif name.startswith("orchestration."):
                        imports.add(name.split(".")[1])
                elif isinstance(node, ast.Import):
                    imports.update(alias.name.split(".")[1] for alias in node.names
                                   if alias.name.startswith("orchestration."))
            with self.subTest(module=module):
                self.assertLessEqual(imports, dependencies)

    def test_imports_in_fresh_processes(self):
        for order in (("sdk", "cloud_client", "cloud", "cloud_app", "runner"),
                      ("cloud_app", "cloud", "cloud_client", "sdk", "runner")):
            code = "import importlib; " + "; ".join(
                f"importlib.import_module('orchestration.{name}')" for name in order)
            result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_worker_cli_routes_to_cloud_client(self):
        with patch("sys.argv", ["orchestration", "worker", "--poll", "3"]), \
                patch("orchestration.cloud_client.worker") as worker:
            runner.main()
        worker.assert_called_once_with(ROOT / "outputs", poll=3)

    def test_backend_reuses_model_handle_for_same_spec(self):
        from unittest.mock import Mock
        backend = sdk.Backend.__new__(sdk.Backend)
        backend.modal = Mock()
        backend.models = {}
        spec = {"service": "example", "app_name": "example-app", "modal_class": "Model"}
        backend.submit(spec, {"id": "one"}, "prefix", "cache")
        backend.submit(spec, {"id": "two"}, "prefix", "cache")
        backend.modal.Cls.from_name.assert_called_once_with("example-app", "Model")
        self.assertEqual(backend.modal.Cls.from_name.return_value.return_value.generate_one.spawn.call_count, 2)
