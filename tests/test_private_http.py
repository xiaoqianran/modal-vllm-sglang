"""Offline safety regression: deployed HTTP services must require proxy auth."""
import ast
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.http import modal_proxy_headers

ROOT = Path(__file__).resolve().parents[1]
APPS = ("001-moss-image", "002-pika-h3-flex", "005-minimax-h3-image")


class PrivateHttpTests(unittest.TestCase):
    def test_every_modal_web_server_requires_proxy_auth(self):
        for folder in APPS:
            with self.subTest(folder=folder):
                tree = ast.parse((ROOT / folder / "app.py").read_text(encoding="utf-8"))
                servers = [
                    decorator
                    for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    for decorator in node.decorator_list
                    if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr == "web_server"
                ]
                self.assertEqual(len(servers), 1)
                self.assertTrue(any(keyword.arg == "requires_proxy_auth"
                                    and isinstance(keyword.value, ast.Constant)
                                    and keyword.value.value is True
                                    for keyword in servers[0].keywords))

    def test_all_http_clients_have_proxy_token_mapping(self):
        services = json.loads((ROOT / "endpoints.example.json").read_text(encoding="utf-8"))["services"]
        for name in ("moss", "pika", "minimax"):
            with self.subTest(service=name):
                self.assertEqual(services[name]["headers_env"], {
                    "Modal-Key": f"{name.upper()}_MODAL_KEY",
                    "Modal-Secret": f"{name.upper()}_MODAL_SECRET",
                })

    def test_minimax_snapshot_has_class_lifecycle(self):
        tree = ast.parse((ROOT / "005-minimax-h3-image/app.py").read_text(encoding="utf-8"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "H3")
        hooks = [
            (node.name, decorator.func.attr, {
                kw.arg: kw.value.value for kw in decorator.keywords
                if isinstance(kw.value, ast.Constant)
            })
            for node in cls.body if isinstance(node, ast.FunctionDef)
            for decorator in node.decorator_list if isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
        ]
        self.assertIn(("start", "enter", {}), hooks)
        self.assertIn(("check_restored", "enter", {"snap": False}), hooks)
        self.assertIn(("stop", "exit", {}), hooks)
        self.assertTrue(any(name == "serve" and kind == "web_server" for name, kind, _ in hooks))
        self.assertIn(("serve", "web_server", {
            "label": "modal-forge-005-minimax-h3-image-serve",
            "startup_timeout": 3600,
            "requires_proxy_auth": True,
        }), hooks)
        start = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "start")
        decorator = next(d for d in start.decorator_list if isinstance(d, ast.Call))
        self.assertTrue(any(kw.arg == "snap" and isinstance(kw.value, ast.Name)
                            and kw.value.id == "GPU_SNAPSHOT" for kw in decorator.keywords))

    def test_pika_tokens_required(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            modal_proxy_headers("PIKA")
        with patch.dict(os.environ, {"PIKA_MODAL_KEY": "key", "PIKA_MODAL_SECRET": "secret"}, clear=True):
            self.assertEqual(modal_proxy_headers("PIKA"), {
                "Modal-Key": "key", "Modal-Secret": "secret",
            })


if __name__ == "__main__":
    unittest.main()
