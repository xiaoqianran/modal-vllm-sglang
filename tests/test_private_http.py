"""Offline safety regression: deployed HTTP services must require proxy auth."""
import ast
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime.utils.http import modal_proxy_headers

ROOT = Path(__file__).resolve().parents[1]
APPS = ("models/001-moss-image", "models/002-pika-h3-flex", "models/005-minimax-h3-image")


class PrivateHttpTests(unittest.TestCase):
    def test_every_modal_web_server_requires_proxy_auth(self):
        tree = ast.parse((ROOT / "runtime/serving/model_runtime.py").read_text(encoding="utf-8"))
        servers = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                   and isinstance(node.func, ast.Attribute) and node.func.attr == "web_server"]
        # The source-imported method and local registration both require auth.
        self.assertEqual(len(servers), 2)
        for server in servers:
            self.assertTrue(any(k.arg == "requires_proxy_auth" and isinstance(k.value, ast.Constant)
                                and k.value.value is True for k in server.keywords))

    def test_all_http_clients_have_proxy_token_mapping(self):
        services = json.loads((ROOT / "endpoints.example.json").read_text(encoding="utf-8"))["services"]
        for name in ("moss", "pika", "minimax"):
            with self.subTest(service=name):
                self.assertEqual(services[name]["headers_env"], {
                    "Modal-Key": f"{name.upper()}_MODAL_KEY",
                    "Modal-Secret": f"{name.upper()}_MODAL_SECRET",
                })

    def test_pika_tokens_required(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            modal_proxy_headers("PIKA")
        with patch.dict(os.environ, {"PIKA_MODAL_KEY": "key", "PIKA_MODAL_SECRET": "secret"}, clear=True):
            self.assertEqual(modal_proxy_headers("PIKA"), {
                "Modal-Key": "key", "Modal-Secret": "secret",
            })


if __name__ == "__main__":
    unittest.main()
