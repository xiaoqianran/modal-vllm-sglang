"""MiniMax cache integrity and existing runner integration; no GPU."""
import unittest
from orchestration import model_capabilities as adapters


class MiniMaxTests(unittest.TestCase):
    def test_parameters_reject_wrong_schedule_and_foreign_options(self):
        for params in ({"num_inference_steps": 5}, {"num_inference_steps": True},
                       {"guidance_scale": 3}, {"size": "512x512"}, {"size": "2049x2048"}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                adapters.parameters("minimax.t2i", params)
        self.assertEqual(adapters.parameters("minimax.t2i", {"size": "1024x1024"})["size"], "1024x1024")
        self.assertEqual(adapters.parameters("minimax.t2i", {"size": "5472x3072"})["size"], "5472x3072")
        with self.assertRaises(ValueError):
            adapters.parameters("unknown.t2i", {})
