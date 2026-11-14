"""MiniMax cache integrity and existing runner integration; no GPU."""
import base64
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestration import adapters, runner
from shared.cache import file_inventory

class MiniMaxTests(unittest.TestCase):
    def test_parameters_reject_wrong_schedule_and_foreign_options(self):
        for params in ({"num_inference_steps": 5}, {"num_inference_steps": True},
                       {"guidance_scale": 3}, {"size": "1024x1024"}, {"size": "2049x2048"}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                adapters.parameters("minimax.t2i", params)
        self.assertEqual(adapters.parameters("minimax.t2i", {"size": "5472x3072"})["size"], "5472x3072")
        with self.assertRaises(ValueError):
            adapters.parameters("unknown.t2i", {})

    def test_real_adapter_image_protocol_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("tasks", "workflows"):
                shutil.copytree(runner.ROOT / name, root / name)
            shutil.copy(runner.ROOT / "endpoints.example.json", root)
            plan = runner.build_plan("tasks/005-minimax-image-demo.json", root=root)
            self.assertEqual(plan["jobs"][0]["params"]["num_inference_steps"], 6)
            png = b"\x89PNG\r\n\x1a\n" + b"test" * 20
            env = {"MINIMAX_ENDPOINT": "http://minimax.test", "MINIMAX_MODAL_KEY": "key",
                   "MINIMAX_MODAL_SECRET": "secret"}
            with patch.dict(os.environ, env), patch("shared.inference_api.request_json",
                    return_value={"data": [{"b64_json": base64.b64encode(png).decode()}]}) as request:
                directory = runner.new_run(plan, root=root)
                self.assertEqual(runner.execute(directory)["counts"], {"completed": 1})
                self.assertEqual((directory / "001.png").read_bytes(), png)
                runner.execute(directory)
                request.assert_called_once()
                self.assertEqual(request.call_args.args[0], "http://minimax.test/v1/images/generations")
                payload = request.call_args.args[1]
                self.assertEqual(payload["seed"], 42)
                self.assertNotIn("guidance_scale", payload)
