"""Behavior checks shared by MOSS, Pika, MiniMax and future model specs."""
import base64
import copy
import hashlib
import json
import tempfile
import unittest
import sys
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

from shared.tasks.batch import intent_digest
from shared.utils.files import atomic_json, read_json
from shared.config.model_cache import component_specs, verify_cache, checked_inventory, command_argv, prepare_cache
from shared.serving.inference_execution import execute
from shared.serving.model_runtime import build_image
from shared.tasks.task_spec import prepare
from orchestration.runner import build_plan
import shutil
from shared.config.model_spec import discover, parameters
from shared.serving.inference_gateway import prepare_request

ROOT = Path(__file__).resolve().parents[1]
SPECS = discover()
PNG = b"\x89PNG\r\n\x1a\n" + b"image-content" * 12
MP4 = b"\0\0\0\x20ftyp" + b"video-content" * 12


class UnifiedModelsTests(unittest.TestCase):
    def test_plan_item_overrides_reach_model_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.task(root, params={"guidance_scale": 4}, items=[
                {"id": "001", "prompt": "boat", "seed": 42, "params": {"guidance_scale": 5}}])
            job = build_plan(path, root=root)["jobs"][0]
            from orchestration.sdk import item_for
            item = item_for(root, job, {}, SPECS["moss.t2i"], None)
            payload, _ = prepare(SPECS["moss.t2i"], item)
            self.assertEqual(payload["seed"], 42)
            self.assertEqual(payload["guidance_scale"], 5)

    def test_container_cache_environment_uses_linux_paths_on_windows_hosts(self):
        with patch("shared.serving.modal_setup.runtime_image") as factory:
            build_image(SPECS["pika.t2va"], offline=True)
            env = factory.call_args.kwargs["env"]
            self.assertEqual(env["HF_HOME"], "/mnt/modal/009-pika-h3-flex")
            self.assertEqual(env["SGLANG_DIFFUSION_CACHE_ROOT"], "/mnt/modal/009-pika-h3-flex/sglang")
            self.assertEqual(env["HF_HUB_OFFLINE"], "1")
            build_image(SPECS["pika.t2va"], offline=False)
            self.assertEqual(factory.call_args.kwargs["env"]["HF_HUB_OFFLINE"], "0")

    def test_failed_cpu_refresh_does_not_publish_a_partial_cache_manifest(self):
        spec = SPECS["moss.t2i"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "hub/snapshots/first"
            snapshot.mkdir(parents=True)
            (snapshot / "weights.safetensors").write_bytes(b"weights")
            (snapshot / "model_index.json").write_text("{}")
            download = Mock(return_value=str(snapshot))
            with patch.dict(sys.modules, {"huggingface_hub": SimpleNamespace(snapshot_download=download)}):
                prepare_cache(spec, Mock(), root)
                original = (root / "model-ready.json").read_bytes()
                download.side_effect = OSError("download interrupted")
                with self.assertRaises(OSError):
                    prepare_cache(spec, Mock(), root, refresh=True)
                self.assertEqual((root / "model-ready.json").read_bytes(), original)
                verify_cache(spec, root)

    def test_cpu_hf_preparation_freezes_resolved_snapshot_and_reuses_cache(self):
        spec = SPECS["moss.t2i"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "hub/models--OpenMOSS-Team--MOSS-Image-SFT/snapshots/resolved-commit"
            snapshot.mkdir(parents=True)
            (snapshot / "weights.safetensors").write_bytes(b"weights")
            (snapshot / "model_index.json").write_text("{}")
            download = Mock(return_value=str(snapshot))
            with patch.dict(sys.modules, {"huggingface_hub": SimpleNamespace(snapshot_download=download)}):
                volume = Mock()
                first = prepare_cache(spec, volume, root)
                second = prepare_cache(spec, volume, root)
            self.assertEqual(first["fingerprint"], second["fingerprint"])
            download.assert_called_once()
            self.assertEqual(download.call_args.kwargs["revision"], spec["revision"])
            self.assertEqual(read_json(root / "model-ready.json")["components"]["model"]["resolved_revision"], "resolved-commit")
            volume.commit.assert_called_once()

    def test_preparation_accepts_download_path_resolved_against_mount_alias(self):
        spec = SPECS["moss.t2i"]
        with tempfile.TemporaryDirectory() as directory:
            real_root = Path(directory).resolve()
            (real_root / "mount-alias").mkdir()
            root = real_root / "mount-alias" / ".."
            snapshot = real_root / "hub/snapshots/commit"
            snapshot.mkdir(parents=True)
            (snapshot / "weights.safetensors").write_bytes(b"weights")
            (snapshot / "model_index.json").write_text("{}")
            with patch.dict(sys.modules, {"huggingface_hub": SimpleNamespace(snapshot_download=Mock(return_value=str(snapshot)))}):
                result = prepare_cache(spec, Mock(), root)
            self.assertTrue(result["ready"])
            self.assertEqual(read_json(root / "model-ready.json")["components"]["model"]["path"], "hub/snapshots/commit")

    def test_cpu_overlay_materialization_and_required_marker(self):
        spec = SPECS["pika.t2va"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            materialized = root / "sglang/materialized_models/pika"
            materialized.mkdir(parents=True)
            (materialized / "weights.safetensors").write_bytes(b"weights")
            (materialized / "model_index.json").write_text("{}")
            marker = materialized / ".sglang_overlay_materialized.json"
            marker.write_text("{}")
            materialize = Mock(return_value=str(materialized))
            hf_diffusers = SimpleNamespace(maybe_download_model=materialize, snapshot_download=Mock())
            modules = {"huggingface_hub": SimpleNamespace(snapshot_download=Mock()),
                       "sglang": SimpleNamespace(),
                       "sglang.multimodal_gen": SimpleNamespace(),
                       "sglang.multimodal_gen.runtime": SimpleNamespace(),
                       "sglang.multimodal_gen.runtime.utils": SimpleNamespace(hf_diffusers_utils=hf_diffusers),
                       "sglang.multimodal_gen.runtime.utils.hf_diffusers_utils": hf_diffusers}
            with patch.dict(sys.modules, modules):
                prepare_cache(spec, Mock(), root)
                materialize.assert_called_once_with(
                    spec["model"], force_diffusers_model=True, revision=spec["revision"])
            paths, _ = verify_cache(spec, root)
            argv = command_argv(spec, paths)
            self.assertEqual(argv[argv.index("--model-path") + 1], spec["model"])
            marker.unlink()
            with self.assertRaises(RuntimeError):
                verify_cache(spec, root)

    def task(self, root, service="moss", operation="t2i", **updates):
        task = {"version": 1, "name": "006-demo", "workflow": f"{service}-{operation}",
                "items": [{"id": "001", "prompt": "boat", "seed": 42}], **updates}
        path = root / "tasks" / "006-demo.json"
        atomic_json(path, task)
        shutil.copytree(ROOT / "workflows", root / "workflows", dirs_exist_ok=True)
        return path

    def test_all_capabilities_are_discovered_from_model_json(self):
        self.assertEqual(set(SPECS), {"moss.t2i", "pika.t2va", "pika.fl2va", "minimax.t2i"})
        self.assertEqual(SPECS["pika.fl2va"]["inputs"]["first_frame"], "image")
        self.assertEqual(parameters(SPECS["pika.fl2va"], {})["task"], "fl2va")

    def test_invalid_model_params_rejected_by_both_entrypoints(self):
        for capability, overrides in (("moss.t2i", {"typo": 1}), ("moss.t2i", {"guidance_scale": 0}),
                ("minimax.t2i", {"size": "1024x1024"}), ("minimax.t2i", {"num_inference_steps": True}),
                ("pika.t2va", {"num_inference_steps": 7}), ("pika.t2va", {"seconds": 4})):
            spec = SPECS[capability]
            with self.subTest(capability=capability, params=overrides):
                with self.assertRaises(ValueError):
                    parameters(spec, overrides)
                with self.assertRaises(ValueError):
                    prepare(spec, {"id": "001", "prompt": "boat", "seed": 42, "params": overrides})

    def test_task_params_and_stable_implicit_seed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = {"params": {"guidance_scale": 4}, "items": [
                {"id": "001", "prompt": "boat"},
                {"id": "002", "prompt": "lake", "params": {"guidance_scale": 5}}]}
            path = self.task(root, **task)
            before = build_plan(path, root=root)["jobs"]
            self.task(root, **{**task, "items": list(reversed(task["items"]))})
            after = build_plan(path, root=root)["jobs"]
            self.assertEqual({i["item"]: i["inputs"]["seed"] for i in before}, {i["item"]: i["inputs"]["seed"] for i in after})
            self.assertEqual(before[1]["params"]["guidance_scale"], 5)
            invalid = {"items": [task["items"][0], {"id": "002", "prompt": "bad", "seed": True}]}
            self.task(root, **invalid)
            with self.assertRaises(ValueError):
                build_plan(path, root=root)

    def test_keyframe_content_changes_plan_and_payload_identity(self):
        spec = SPECS["pika.fl2va"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "first.png").write_bytes(PNG)
            (root / "last.png").write_bytes(PNG + b"last")
            path = self.task(root, "pika", "fl2va", items=[{"id": "001", "prompt": "boat", "seed": 42,
                             "first_frame": {"file": "first.png"}, "last_frame": {"file": "last.png"}}])
            plan = build_plan(path, root=root)
            item = {"id": "001", "operation": "fl2va", "prompt": "boat", "seed": 42,
                    "first_frame_base64": base64.b64encode(PNG).decode(),
                    "last_frame_base64": base64.b64encode(PNG + b"last").decode()}
            payload, signature = prepare(spec, item)
            with patch("shared.serving.inference_gateway.FRAME_DIR", root / "staged"):
                staged = prepare_request(payload, spec)
            self.assertEqual([c["frame_index"] for c in staged["conditions"]], [0, -1])
            self.assertEqual(staged["conditions"][0]["role"], "keyframe")
            (root / "last.png").write_bytes(PNG + b"changed")
            self.assertNotEqual(plan["hash"], build_plan(path, root=root)["hash"])
            item["last_frame_base64"] = base64.b64encode(PNG + b"changed").decode()
            self.assertNotEqual(signature, prepare(spec, item)[1])
            with self.assertRaises(ValueError):
                prepare_request({**payload, "flow_shift": 5}, spec)
            with self.assertRaises(ValueError):
                prepare_request({**payload, "last_frame_base64": "bad"}, spec)
            with self.assertRaises(ValueError):
                prepare_request({**payload, "task": "t2va"}, SPECS["pika.t2va"])

    def test_both_minimax_components_and_command_paths(self):
        spec = SPECS["minimax.t2i"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = {"version": 1, "cache_spec": intent_digest(component_specs(spec)), "components": {}}
            for name, config in component_specs(spec).items():
                path = root / name
                files = ["model_index.json", "pdd_config.json", "pdd_fused_heads.safetensors", "transformer/weights.safetensors", "vae/weights.safetensors"] if name == "model" else ["text_encoder/weights.safetensors"]
                for filename in files:
                    target = path / filename
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(b"weights")
                record["components"][name] = {"path": name, "inventory": checked_inventory(path, config)}
            atomic_json(root / "model-ready.json", record)
            paths, fingerprint = verify_cache(spec, root)
            argv = command_argv(spec, paths)
            self.assertEqual(argv[argv.index("--text-encoder-path") + 1], str(paths["encoder"]) + "/text_encoder")
            (root / "encoder/text_encoder/weights.safetensors").write_bytes(b"damaged-weights")
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                verify_cache(spec, root)
            record["components"]["encoder"]["path"] = "../outside"
            atomic_json(root / "model-ready.json", record)
            with self.assertRaisesRegex(RuntimeError, "Invalid"):
                verify_cache(spec, root)


    def test_remote_image_publish_crash_can_finish_without_regeneration(self):
        spec = SPECS["moss.t2i"]
        item = {"id": "001", "prompt": "boat", "seed": 42}
        with tempfile.TemporaryDirectory() as directory, patch("shared.serving.inference_execution.Path", wraps=Path) as paths:
            paths.side_effect = lambda value: Path(directory) if value == "/mnt/modal" else Path(value)
            with patch("shared.serving.inference_api.VLLMImageClient") as factory:
                factory.return_value.generate.return_value = (PNG, ".png")
                original = atomic_json
                def crash(path, record):
                    if record.get("status") == "completed":
                        raise OSError("client died after file publish")
                    original(path, record)
                with patch("shared.serving.inference_execution.atomic_json", side_effect=crash), self.assertRaises(OSError):
                    list(execute(spec, [item], "batch", Mock()))
                result = list(execute(spec, [item], "batch", Mock()))
                self.assertEqual(result[0]["status"], "completed")
                factory.return_value.generate.assert_called_once()
