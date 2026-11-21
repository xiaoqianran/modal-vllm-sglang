"""Synthetic Pika bridge tests: no Modal containers or model downloads."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from shared.config.model_spec import validate
from shared.config.model_cache import command_argv, verify_cache
from shared.config.pika_overlay import (
    BASE_REPO, BASE_REV, BASE_PATTERNS, OVERLAY_REPO, OVERLAY_REV,
    SOURCE_REPO, SOURCE_REV, download_sources, materialize_with_sglang,
    publish_pika_cache,
)
from shared.utils.files import read_json


class PikaOverlayTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        self.spec = validate(read_json(root / "002-pika-h3-flex/model.json"))

    def fixture(self, root):
        base = root / "azure" / "run"
        source_pin = {"repo_id": BASE_REPO, "revision": BASE_REV}
        files = {
            SOURCE_REPO: {
                "fl2va/manifest.json": json.dumps({"base": source_pin}).encode(),
                "ref2va/manifest.json": json.dumps({"base": source_pin}).encode(),
                "fl2va/adapter.safetensors": b"adapter-a",
                "ref2va/adapter.safetensors": b"adapter-b",
            },
            OVERLAY_REPO: {
                "_overlay/overlay_manifest.json": json.dumps({
                    "source_model_id": SOURCE_REPO,
                    "source_revision": SOURCE_REV,
                    "custom_materializer": "_overlay/materialize.py",
                    "required_source_files": [
                        "fl2va/manifest.json", "ref2va/manifest.json",
                        "fl2va/adapter.safetensors", "ref2va/adapter.safetensors"
                    ],
                }).encode(),
                "_overlay/materialize.py": b"# upstream hook",
            },
            BASE_REPO: {
                "FL2VA/text_encoder/model.safetensors": b"base-1",
                "Ref2VA/text_encoder/model.safetensors": b"base-2",
            },
        }
        revisions = {SOURCE_REPO: SOURCE_REV, OVERLAY_REPO: OVERLAY_REV, BASE_REPO: BASE_REV}
        manifest = {"version": 1, "models": []}
        for repo, items in files.items():
            path = base / repo / revisions[repo] / "model"
            for name, data in items.items():
                target = path / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            manifest["models"].append({
                "repo_id": repo, "revision": revisions[repo],
                "files": {name: {"size": len(data)} for name, data in items.items()}})
        return base, manifest

    def test_download_plan_pins_three_repositories_and_filters_large_export(self):
        sources = download_sources(self.spec)
        self.assertEqual([s["repo_id"] for s in sources],
                         [SOURCE_REPO, OVERLAY_REPO, BASE_REPO])
        self.assertEqual(sources[0]["revision"], SOURCE_REV)
        self.assertEqual(sources[1]["revision"], OVERLAY_REV)
        self.assertEqual(sources[2]["revision"], BASE_REV)
        self.assertIn("FL2VA/transformer/**", sources[2]["include"])
        self.assertIn("Ref2VA/transformer/**", sources[2]["include"])
        self.assertNotIn("comfy/loras", sources[0]["include"])
        self.assertEqual(len(BASE_PATTERNS), 12)

    def test_publish_reuses_inference_contract_and_local_gpu_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base, manifest = self.fixture(root)
            volume = Mock()

            def materialize(source, overlay, weights):
                self.assertTrue((source / "fl2va/adapter.safetensors").is_file())
                self.assertTrue((overlay / "_overlay/materialize.py").is_file())
                self.assertTrue((weights / "FL2VA/text_encoder/model.safetensors").is_file())
                output = root / "sglang/materialized_models/pika"
                output.mkdir(parents=True)
                (output / "model_index.json").write_bytes(b'{}')
                (output / "model.safetensors").write_bytes(b"final")
                (output / ".sglang_overlay_materialized.json").write_text(json.dumps({
                    "source_model_id": SOURCE_REPO, "source_revision": SOURCE_REV,
                    "overlay_repo_id": OVERLAY_REPO, "overlay_revision": OVERLAY_REV,
                }))
                return output

            result = publish_pika_cache(self.spec, manifest, base, root, volume,
                                        materialize=materialize)
            self.assertTrue(result["ready"])
            paths, fingerprint = verify_cache(self.spec, root)
            self.assertEqual(fingerprint, result["fingerprint"])
            self.assertEqual(command_argv(self.spec, paths)[
                command_argv(self.spec, paths).index("--model-path") + 1], str(paths["model"]))
            self.assertEqual(read_json(root / "model-ready.json")["components"]["model"]["resolved_revision"],
                             SOURCE_REV)
            volume.commit.assert_called_once()

    def test_bad_snapshot_keeps_last_ready_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base, manifest = self.fixture(root)
            marker = root / "model-ready.json"
            marker.write_bytes(b"previous good cache")
            manifest["models"][0]["files"]["fl2va/adapter.safetensors"]["size"] += 1
            volume = Mock()
            with self.assertRaisesRegex(RuntimeError, "snapshot incomplete"):
                publish_pika_cache(self.spec, manifest, base, root, volume)
            self.assertEqual(marker.read_bytes(), b"previous good cache")
            volume.commit.assert_not_called()

    def test_adapter_revision_mismatch_fails_before_materializing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base, manifest = self.fixture(root)
            data = {"base": {"repo_id": BASE_REPO, "revision": "f" * 40}}
            target = base / SOURCE_REPO / SOURCE_REV / "model/fl2va/manifest.json"
            target.write_text(json.dumps(data))
            manifest["models"][0]["files"]["fl2va/manifest.json"]["size"] = target.stat().st_size
            callback = Mock()
            volume = Mock()
            with self.assertRaisesRegex(ValueError, "adapter does not pin"):
                publish_pika_cache(self.spec, manifest, base, root, volume,
                                   materialize=callback)
            callback.assert_not_called()
            volume.commit.assert_not_called()

    def test_sglang_materializer_sees_only_local_prepared_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, overlay, base = [root / name for name in ("source", "overlay", "base")]
            for folder in (source, overlay, base):
                folder.mkdir()
            hub = SimpleNamespace(snapshot_download=Mock(side_effect=AssertionError("network called")))
            def upstream_materialize(**kwargs):
                self.assertEqual(kwargs["source_dir"], str(source))
                self.assertEqual(kwargs["overlay_dir"], str(overlay))
                self.assertEqual(
                    hub.snapshot_download(BASE_REPO, revision=BASE_REV,
                                          allow_patterns=list(BASE_PATTERNS)), str(base))
                with self.assertRaisesRegex(RuntimeError, "Unexpected SGLang snapshot"):
                    hub.snapshot_download("unknown/repository", revision=BASE_REV,
                                          allow_patterns=list(BASE_PATTERNS))
                return str(root / "result")
            overlay_api = SimpleNamespace(
                resolve_model_overlay=lambda _: {"overlay_repo_id": OVERLAY_REPO,
                   "overlay_revision": OVERLAY_REV, "source_revision": SOURCE_REV},
                materialize_overlay_model=upstream_materialize)
            utils = SimpleNamespace(hf_diffusers_utils=SimpleNamespace(
                _verify_diffusers_model_complete=lambda _: True), model_overlay=overlay_api)
            modules = {"huggingface_hub": hub,
                       "sglang": SimpleNamespace(),
                       "sglang.multimodal_gen": SimpleNamespace(),
                       "sglang.multimodal_gen.runtime": SimpleNamespace(),
                       "sglang.multimodal_gen.runtime.utils": utils}
            with patch.dict(sys.modules, modules):
                result = materialize_with_sglang(source, overlay, base)
            self.assertEqual(result, root / "result")
            hub.snapshot_download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
