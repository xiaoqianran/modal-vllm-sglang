import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from shared.config.azure_cache import publish_cache, preparation_strategy
from shared.config.model_cache import verify_cache, command_argv
from shared.utils.files import read_json


class AzureCacheTests(unittest.TestCase):
    def fixture(self, root):
        spec = {"service": "example", "model": "org/model", "revision": "a" * 40,
                "command": ["serve", "{model_path}"],
                "components": {"encoder": {"model": "org/encoder", "revision": "b" * 40}}}
        base = root / "azure" / "run"
        models = {}
        for name, repo, revision in (("model", "org/model", "a" * 40), ("encoder", "org/encoder", "b" * 40)):
            path = base / repo / revision / "model"
            path.mkdir(parents=True)
            (path / "weight.safetensors").write_bytes(b"weights")
            models[name] = {"repo_id": repo, "revision": revision,
                            "files": {"weight.safetensors": {"size": 7}}}
        return spec, models, base


    def test_three_services_dispatch_without_conflating_overlay_with_hf(self):
        from shared.config.model_spec import validate
        from shared.utils.files import read_json
        root = Path(__file__).resolve().parents[1]
        for directory, expected in (("001-moss-image", "hf"),
                                    ("005-minimax-h3-image", "hf"),
                                    ("002-pika-h3-flex", "sglang_overlay")):
            with self.subTest(directory=directory):
                spec = validate(read_json(root / directory / "model.json"))
                self.assertEqual(preparation_strategy(spec), expected)

    def test_mixed_materialization_requires_explicit_adapter(self):
        spec = {"service": "mixed", "model": "org/model", "revision": "main",
                "components": {"encoder": {"model": "org/encoder", "revision": "main",
                                           "mode": "sglang_overlay"}}}
        with self.assertRaisesRegex(ValueError, "Mixed HF"):
            preparation_strategy(spec)

    def test_publish_is_readable_by_existing_inference_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec, models, base = self.fixture(root)
            volume = Mock()
            result = publish_cache(spec, models, base, root, volume)
            paths, fingerprint = verify_cache(spec, root)
            self.assertEqual(result["fingerprint"], fingerprint)
            self.assertEqual(set(paths), {"model", "encoder"})
            self.assertEqual(command_argv(spec, paths), ["serve", str(paths["model"])])
            self.assertFalse(Path(read_json(root / "model-ready.json")["components"]["model"]["path"]).is_absolute())
            volume.commit.assert_called_once()

    def test_incomplete_component_does_not_replace_ready_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec, models, base = self.fixture(root)
            marker = root / "model-ready.json"
            marker.write_text("old ready marker")
            models["encoder"]["files"]["missing.bin"] = {"size": 1}
            volume = Mock()
            with self.assertRaises(RuntimeError):
                publish_cache(spec, models, base, root, volume)
            self.assertEqual(marker.read_text(), "old ready marker")
            volume.commit.assert_not_called()

    def test_final_validation_failure_preserves_last_good_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec, models, base = self.fixture(root)
            volume = Mock()
            publish_cache(spec, models, base, root, volume)
            marker = root / "model-ready.json"
            previous = marker.read_bytes()
            volume.reset_mock()
            with patch("shared.config.azure_cache.verify_cache", side_effect=RuntimeError("validation failed")):
                with self.assertRaisesRegex(RuntimeError, "validation failed"):
                    publish_cache(spec, models, base, root, volume)
            self.assertEqual(marker.read_bytes(), previous)
            volume.commit.assert_not_called()
