"""Offline contracts for modal-benchmark v0.2.2 CLI."""
from __future__ import annotations
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from scripts.download_models import plan, run
from shared.config.model_spec import validate
from shared.utils.files import read_json

ROOT = Path(__file__).resolve().parents[1]


class App:
    def __init__(self):
        self.registered = []
        self.runs = 0

    def function(self, **kwargs):
        def register(fn):
            self.registered.append(kwargs)
            return SimpleNamespace(remote=Mock(side_effect=fn))
        return register

    def run(self):
        self.runs += 1
        return nullcontext()


class DownloadCliTests(TestCase):
    def spec(self, name):
        return validate(read_json(ROOT / name / "model.json"))

    def setup_downloader(self, spec):
        app = App()
        manifest = {"version": 1, "models": [
            {"repo_id": source["repo_id"], "revision": "a" * 40,
             "files": {"weights.bin": {"size": 2}}}
            for source in plan(spec, 2)["sources"]
        ]}
        downloader = SimpleNamespace(
            app=app, volume=SimpleNamespace(reload=Mock()),
            image=SimpleNamespace(add_local_python_source=Mock(return_value=object())),
            download=Mock(return_value={
                "status": "VERIFIED", "manifest": manifest,
                "base": plan(spec, 2)["base"], "files": 2, "bytes": 4,
                "end_to_end_seconds": 1.2,
            }),
        )
        return downloader, app

    def test_free_plan_has_no_legacy_directory(self):
        for name, kind in (("001-moss-image", "hf"),
                           ("005-minimax-h3-image", "hf"),
                           ("002-pika-h3-flex", "sglang_overlay")):
            with self.subTest(name=name):
                result = plan(self.spec(name), 2)
                self.assertEqual(result["strategy"], kind)
                self.assertTrue(result["base"].endswith("/downloads"))
                self.assertNotIn("/azure/", result["base"])
        self.assertEqual(len(plan(self.spec("002-pika-h3-flex"), 2)["sources"]), 3)

    def test_hf_publishes_and_verifies_in_one_rpc(self):
        spec = self.spec("005-minimax-h3-image")
        d, app = self.setup_downloader(spec)
        with (
            patch("modal_benchmark.hf.create_downloader", return_value=d) as create,
            patch("shared.config.azure_cache.publish_cache",
                  return_value={"ready": True, "fingerprint": "cache"}) as publish,
            patch("shared.config.model_cache.verify_cache",
                  return_value=({"model": Path("/mnt/modal/model")}, "cache")) as verify,
        ):
            result = run(spec, workers=2)
        self.assertEqual(result["download"]["status"], "VERIFIED")
        self.assertEqual(app.runs, 1)
        self.assertEqual(len(app.registered), 1)
        publish.assert_called_once()
        verify.assert_called_once()
        self.assertEqual(verify.call_args.kwargs["phase"], "download_persisted")
        d.download.assert_called_once()
        self.assertEqual(d.download.call_args.kwargs["base"], plan(spec, 2)["base"])
        create.assert_called_once()

    def test_pika_keeps_overlay_materialization(self):
        spec = self.spec("002-pika-h3-flex")
        d, app = self.setup_downloader(spec)
        with (
            patch("modal_benchmark.hf.create_downloader", return_value=d),
            patch("shared.serving.model_runtime.build_image", return_value=object()) as image,
            patch("shared.config.pika_overlay.publish_pika_cache",
                  return_value={"ready": True, "fingerprint": "pika"}) as materialize,
            patch("shared.config.model_cache.verify_cache",
                  return_value=({"model": Path("/mnt/modal/pika")}, "pika")),
        ):
            out = run(spec, workers=2)
        self.assertEqual(out["cache"]["fingerprint"], "pika")
        image.assert_called_once_with(spec, offline=True)
        materialize.assert_called_once()
        self.assertEqual(app.runs, 1)
        self.assertEqual(len(app.registered), 1)

    def test_failed_download_never_publishes(self):
        spec = self.spec("001-moss-image")
        d, app = self.setup_downloader(spec)
        d.download.return_value = {"status": "FAILED"}
        with patch("modal_benchmark.hf.create_downloader", return_value=d):
            with self.assertRaisesRegex(RuntimeError, "not verified"):
                run(spec)
        self.assertEqual(app.runs, 0)
