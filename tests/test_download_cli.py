"""Offline contracts for modal-benchmark v0.4.9 CLI."""
from __future__ import annotations
from contextlib import nullcontext
from importlib import import_module
from importlib.metadata import version
from runpy import run_path
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from tools.download_models import cache_implementation, main, plan, run
from runtime.config.model_spec import validate
from runtime.utils.files import read_json

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
    @patch.dict("os.environ", MODEL_VOLUME_NAME="isolated-models")
    def test_download_plan_targets_mounted_volume(self):
        spec = self.spec("models/001-moss-image")
        for storage, expected in (("legacy", "isolated-models"), ("shared", "isolated-models")):
            with self.subTest(storage=storage):
                self.assertEqual(plan({**spec, "model_storage": storage})["volume"], expected)

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
            app=app, volume=SimpleNamespace(reload=Mock(), with_mount_options=Mock(return_value=Mock())),
            image=SimpleNamespace(add_local_python_source=Mock(return_value=object())),
            download=Mock(return_value={
                "status": "VERIFIED", "manifest": manifest,
                "base": plan(spec, 2)["base"], "files": 2, "bytes": 4,
                "end_to_end_seconds": 1.2,
            }),
        )
        return downloader, app

    def test_volume_v2_uses_persistent_writer_defaults(self):
        from modal_benchmark.hf import create_downloader
        self.assertEqual(version("modal-benchmark"), "0.4.9")
        from runtime.config.storage import VOLUME_MOUNT, VOLUME_VERSION, VOLUME_SUB_PATH
        for name in ("models/001-moss-image", "models/002-pika-h3-flex",
                     "models/005-minimax-h3-image"):
            with self.subTest(name=name):
                task = plan(self.spec(name))
                downloader = create_downloader(
                    volume_name=task["volume"], mount_path=VOLUME_MOUNT,
                    sub_path=VOLUME_SUB_PATH, volume_version=VOLUME_VERSION,
                    max_workers=task["workers"], verification_mode="xet",
                )
                config = downloader.config
                self.assertEqual(config.scheduling, "persistent")
                self.assertEqual(config.task_grouping, "hybrid")
                self.assertFalse(config.range_work_stealing)
                self.assertEqual(config.task_batch_bytes, 10 * 1024**3)
                self.assertEqual(config.commit_batch_files, 16)
                self.assertEqual(config.commit_batch_bytes, 16 * 1024**3)
                self.assertEqual(config.small_file_workers, 4)
                self.assertTrue(config.coalesce_commits)
                self.assertEqual(config.xet_concurrency, 8)
                self.assertEqual(config.replacement_budget, 0)
                self.assertEqual(config.volume_version, 2)
                self.assertEqual(config.sub_path, "/inference")
                self.assertEqual(config.max_workers, task["workers"])

    def test_free_plan_has_no_legacy_directory(self):
        for name, kind in (("models/001-moss-image", "hf"),
                           ("models/005-minimax-h3-image", "hf"),
                           ("models/002-pika-h3-flex", "sglang_overlay")):
            with self.subTest(name=name):
                result = plan(self.spec(name), 2)
                self.assertEqual(result["strategy"], kind)
                self.assertTrue(result["base"].endswith("/downloads"))
                self.assertNotIn("/azure/", result["base"])
        self.assertEqual(len(plan(self.spec("models/002-pika-h3-flex"), 2)["sources"]), 3)

    def test_cache_plugin_is_declared_by_model_not_python_search_path(self):
        spec = self.spec("models/002-pika-h3-flex")
        self.assertEqual(spec["cache_implementation"], "models.002-pika-h3-flex.model_impl")
        self.assertEqual(cache_implementation(spec).__name__, spec["cache_implementation"])
        self.assertNotIn("models/002-pika-h3-flex", __import__("sys").path)
        for value in ("model_impl", "../models.002-pika-h3-flex.model_impl", "models.001-moss-image.model_impl"):
            bad = {**spec, "cache_implementation": value}
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate(bad)
        with self.assertRaisesRegex(ValueError, "cache_implementation"):
            plan({**spec, "cache_implementation": None})

    def test_pika_download_model_is_a_free_cli_facade(self):
        entrypoint = ROOT / "models/002-pika-h3-flex" / "download_model.py"
        with patch("tools.download_models.main") as delegate:
            module = run_path(str(entrypoint))
            module["main"]([])
            self.assertEqual(delegate.call_count, 1)
            self.assertEqual(delegate.call_args.args[0], [str(entrypoint.with_name("model.json"))])

    def test_all_model_download_entrypoints_use_one_cli(self):
        for folder in ("models/001-moss-image", "models/002-pika-h3-flex",
                       "models/005-minimax-h3-image", "models/006-qwen38-pi"):
            with self.subTest(folder=folder), patch("tools.download_models.main") as delegate:
                entrypoint = ROOT / folder / "download_model.py"
                module = run_path(str(entrypoint))
                module["main"](["--check-only"])
                delegate.assert_called_once_with([
                    str(entrypoint.with_name("model.json")), "--check-only",
                ])

    def test_cache_modes_do_not_download_or_build_gpu_images(self):
        model = str(ROOT / "models/002-pika-h3-flex" / "model.json")
        for flag in ("--check-only", "--migrate-only"):
            with (
                self.subTest(flag=flag),
                patch("tools.download_models.run_cache_maintenance",
                      return_value={"ready": True}) as maintenance,
                patch("tools.download_models.run") as downloader,
            ):
                main([model, flag])
                maintenance.assert_not_called()
                main([model, flag, "--execute"])
                maintenance.assert_called_once_with(self.spec("models/002-pika-h3-flex"),
                                                    migrate_only=flag == "--migrate-only")
                downloader.assert_not_called()

    def test_legacy_qwen_requires_extra_download_opt_in(self):
        model = str(ROOT / "models/006-qwen38-pi" / "model.json")
        with patch("tools.download_models.run") as download:
            main([model])
            with self.assertRaises(SystemExit):
                main([model, "--execute"])
            download.assert_not_called()
            download.return_value = {"download": {"status": "VERIFIED"}}
            main([model, "--execute", "--allow-archived-download"])
            main([model, "--execute", "--allow-legacy-qwen"])
            self.assertEqual(download.call_count, 2)

    def test_invalid_download_policy_rejected(self):
        spec = self.spec("models/006-qwen38-pi")
        with self.assertRaisesRegex(ValueError, "download_policy"):
            validate({**spec, "download_policy": "random"})

    def test_check_and_migrate_are_mutually_exclusive(self):
        model = str(ROOT / "models/001-moss-image" / "model.json")
        with patch("tools.download_models.run_cache_maintenance") as maintenance:
            with self.assertRaises(SystemExit):
                main([model, "--check-only", "--migrate-only", "--execute"])
            maintenance.assert_not_called()

    def test_per_model_worker_defaults_and_cli_override(self):
        expected = {"models/001-moss-image": 5,
                    "models/005-minimax-h3-image": 6,
                    "models/002-pika-h3-flex": 8}
        for folder, workers in expected.items():
            with self.subTest(folder=folder):
                spec = self.spec(folder)
                self.assertEqual(plan(spec)["workers"], workers)
                self.assertEqual(plan(spec, 1)["workers"], 1)
        for invalid in (True, 0, 9, 2.5, "3"):
            with self.subTest(invalid=invalid):
                spec = self.spec("models/001-moss-image")
                spec["download_workers"] = invalid
                with self.assertRaisesRegex(ValueError, "download_workers"):
                    validate(spec)

    def test_worker_override_precedence(self):
        spec = self.spec("models/001-moss-image")
        with patch.dict("os.environ", MODEL_HF_DOWNLOAD_WORKERS="8"):
            self.assertEqual(plan(spec)["workers"], 8)
            self.assertEqual(plan(spec, 2)["workers"], 2)
        with patch.dict("os.environ", MODEL_HF_DOWNLOAD_WORKERS=""):
            self.assertEqual(plan({k: v for k, v in spec.items() if k != "download_workers"})["workers"], 3)
            self.assertEqual(plan(validate({**spec, "download_workers": 8}))["workers"], 8)
        for value in ("0", "9", "bad"):
            with patch.dict("os.environ", MODEL_HF_DOWNLOAD_WORKERS=value):
                with self.assertRaises(ValueError):
                    plan(spec)

    def test_model_names_match_directories(self):
        for folder in ("001-moss-image", "002-pika-h3-flex", "005-minimax-h3-image"):
            spec = self.spec("models/" + folder)
            self.assertEqual(spec["service_directory"], folder)
            self.assertEqual(spec["app_name"], folder)
            self.assertEqual(spec["endpoint_label"], folder + "-serve")
            self.assertEqual(spec["cache_directory"], "/data/models/" + folder)

    def test_hf_publishes_and_verifies_in_one_rpc(self):
        spec = self.spec("models/005-minimax-h3-image")
        d, app = self.setup_downloader(spec)
        with (
            patch("modal_benchmark.hf.create_downloader", return_value=d) as create,
            patch("modal.Volume.from_name", return_value=d.volume),
            patch("runtime.config.cache_publication.publish_cache",
                  return_value={"ready": True, "fingerprint": "cache"}) as publish,
            patch("runtime.config.model_cache.verify_cache",
                  return_value=({"model": Path("/data/model")}, "cache")) as verify,
        ):
            result = run(spec, workers=2)
        self.assertEqual(create.call_args.kwargs["volume_version"], 2)
        self.assertEqual(create.call_args.kwargs["mount_path"], "/data")
        self.assertEqual(create.call_args.kwargs["sub_path"], "/inference")
        self.assertEqual(create.call_args.kwargs["app"].name, "download-005-minimax-h3-image")
        self.assertEqual(result["download"]["status"], "VERIFIED")
        self.assertEqual(app.runs, 1)
        self.assertEqual(len(app.registered), 1)
        self.assertFalse(app.registered[0]["serialized"])
        publish.assert_called_once()
        verify.assert_called_once()
        self.assertEqual(verify.call_args.kwargs["phase"], "download_persisted")
        d.download.assert_called_once()
        self.assertEqual(d.download.call_args.kwargs["base"], plan(spec, 2)["base"])
        create.assert_called_once()

    def test_pika_keeps_overlay_materialization(self):
        spec = self.spec("models/002-pika-h3-flex")
        d, app = self.setup_downloader(spec)
        sequence = []
        d.download.side_effect = lambda *a, **kw: (
            sequence.append("download"), {
                "status": "VERIFIED", "manifest": {"models": []},
                "base": plan(spec)["base"], "files": 2, "bytes": 4,
                "end_to_end_seconds": 1.2,
            })[1]
        def build(*args, **kwargs):
            sequence.append("sglang_image")
            return object()
        with (
            patch("modal_benchmark.hf.create_downloader", return_value=d),
            patch("modal.Volume.from_name", return_value=d.volume),
            patch.object(import_module("models.002-pika-h3-flex.model_impl.materializer_image"),
                         "build_pika_materializer_image", side_effect=build) as image,
            patch.object(import_module("models.002-pika-h3-flex.model_impl.overlay"), "publish_pika_cache",
                  return_value={"ready": True, "fingerprint": "pika"}) as materialize,
            patch("runtime.config.model_cache.verify_cache",
                  return_value=({"model": Path("/data/pika")}, "pika")),
        ):
            out = run(spec, workers=2)
        self.assertEqual(out["cache"]["fingerprint"], "pika")
        image.assert_called_once_with(spec, modal_version=version("modal"))
        materialize.assert_called_once()
        self.assertEqual(sequence, ["download", "sglang_image"])
        self.assertEqual(app.runs, 1)
        self.assertEqual(len(app.registered), 1)
        self.assertFalse(app.registered[0]["serialized"])

    def test_failed_pika_download_never_builds_sglang_image(self):
        spec = self.spec("models/002-pika-h3-flex")
        d, app = self.setup_downloader(spec)
        d.download.return_value = {"status": "FAILED"}
        with (
            patch("modal_benchmark.hf.create_downloader", return_value=d),
            patch.object(import_module("models.002-pika-h3-flex.model_impl.materializer_image"),
                         "build_pika_materializer_image") as build,
        ):
            with self.assertRaisesRegex(RuntimeError, "not verified"):
                run(spec)
        build.assert_not_called()
        self.assertEqual(app.runs, 0)

    def test_failed_download_never_publishes(self):
        spec = self.spec("models/001-moss-image")
        d, app = self.setup_downloader(spec)
        d.download.return_value = {"status": "FAILED"}
        with patch("modal_benchmark.hf.create_downloader", return_value=d):
            with self.assertRaisesRegex(RuntimeError, "not verified"):
                run(spec)
        self.assertEqual(app.runs, 0)
