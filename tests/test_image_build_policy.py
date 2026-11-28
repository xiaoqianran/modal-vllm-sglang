"""Offline regression tests for pinned engines and uv-first Modal builds."""
import copy
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime.config.model_spec import validate
from runtime.serving.modal_setup import runtime_image
from runtime.serving.model_runtime import build_image
from runtime.utils.files import read_json


ROOT = Path(__file__).resolve().parents[1]


def spec_for(directory):
    return validate(read_json(ROOT / directory / "model.json"))


class ImageBuildPolicyTests(unittest.TestCase):
    def test_archived_registry_image_is_immutable_digest_reference(self):
        spec = spec_for("models/007-qwen38-pi")
        self.assertRegex(spec["image"], r"^[^\s@]+@sha256:[0-9a-f]{64}$")
        self.assertEqual(spec["download_policy"], "archived")

    def test_sglang_native_services_share_verified_binary_dependency_recipe(self):
        pika = spec_for("models/002-pika-h3-flex")
        minimax = spec_for("models/005-minimax-h3-image-sglang")
        for spec in (pika, minimax):
            with self.subTest(service=spec["service"]):
                self.assertEqual(spec["image"], "debian_slim")
                self.assertEqual(spec["python_version"], "3.12")
                self.assertEqual(spec["uv_torch_backend"], "cu130")
                self.assertEqual(spec["uv_extra_index_url"], "https://docs.sglang.ai/whl/cu130/")
                self.assertIs(spec["uv_only_binary"], True)
                self.assertEqual(len(spec["uv_packages"]), 1)
                self.assertIn("nightly-2026-11-24-1173264b58", spec["uv_packages"][0])
                self.assertRegex(spec["uv_packages"][0], r"\.whl#sha256=[0-9a-f]{64}$")
                self.assertEqual(spec["env"]["CUDA_HOME"], "/usr/local/lib/python3.12/site-packages/nvidia/cu13")
                self.assertIn('ln -s lib "$CUDA_ROOT/lib64"', spec["build_commands"][0])
                self.assertIn('ln -s libcudart.so.13 "$CUDA_ROOT/lib/libcudart.so"', spec["build_commands"][0])
                self.assertEqual(spec["command"][:4],
                                 ["python", "-m", "sglang.multimodal_gen.runtime.entrypoints.cli.main", "serve"])
        self.assertEqual(pika["uv_packages"], minimax["uv_packages"])
        self.assertEqual(pika["uv_requirements"], minimax["uv_requirements"])
        lock = ROOT / "runtime/serving/locks" / pika["uv_requirements"]
        self.assertTrue(lock.is_file())
        with patch("runtime.serving.modal_setup.modal.Image.debian_slim") as native:
            for spec in (pika, minimax):
                build_image(spec, offline=True)
            install = native.return_value.uv_pip_install
            self.assertEqual(install.call_count, 2)
            self.assertEqual(install.call_args_list[0], install.call_args_list[1])
            self.assertEqual(install.call_args.kwargs["requirements"], [str(lock)])
            self.assertEqual(install.call_args.kwargs["uv_version"], "0.12.5")
            self.assertIn("--require-hashes", install.call_args.kwargs["extra_options"])
            self.assertIn("--only-binary=:all:", install.call_args.kwargs["extra_options"])
            self.assertEqual(install.call_args.kwargs["extra_index_url"], pika["uv_extra_index_url"])
        self.assertEqual(pika["build_commands"][0], minimax["build_commands"][0])
        self.assertEqual(len(minimax["build_commands"]), 1)
        self.assertEqual(len(pika["build_commands"]), 2)
        self.assertIn("apt-get install -y --no-install-recommends ffmpeg", pika["build_commands"][1])

    def test_moss_uses_native_modal_and_matching_omni_release(self):
        spec = spec_for("models/001-moss-image")
        self.assertEqual(spec["image"], "debian_slim")
        self.assertEqual(spec["python_version"], "3.12")
        self.assertEqual(spec["uv_packages"], ["vllm==0.34.0", "vllm-omni==0.34.0"])
        self.assertEqual(spec["uv_torch_backend"], "cu130")
        self.assertEqual(spec["build_commands"][:2], ["flashinfer install-cubin-wheel", "flashinfer install-jit-cache-wheel --cuda-version 13.0 --mode minimal --sm sm120"])
        self.assertIn("libxcb1 libgl1 libglib2.0-0", spec["build_commands"][2])
        self.assertIn("import cv2; from vllm_omni.entrypoints.cli.main import main", spec["build_commands"][3])

    def test_omni_services_share_locked_binary_recipe(self):
        moss = spec_for("models/001-moss-image")
        minimax = spec_for("models/006-minimax-h3-image-vllm")
        for field in ("image", "python_version", "uv_packages", "uv_torch_backend",
                      "uv_only_binary", "uv_requirements", "build_commands"):
            self.assertEqual(moss[field], minimax[field], field)
        lock = ROOT / "runtime/serving/locks" / moss["uv_requirements"]
        self.assertTrue(lock.is_file())
        with patch("runtime.serving.modal_setup.modal.Image.debian_slim") as native:
            for spec in (moss, minimax):
                build_image(spec, offline=True)
            install = native.return_value.uv_pip_install
            self.assertEqual(install.call_count, 2)
            self.assertEqual(install.call_args_list[0], install.call_args_list[1])
            self.assertEqual(install.call_args.args, ())
            self.assertEqual(install.call_args.kwargs["requirements"], [str(lock)])
            self.assertEqual(install.call_args.kwargs["uv_version"], "0.12.5")
            self.assertEqual(install.call_args.kwargs["index_url"], "https://pypi.org/simple")
            self.assertIn("--require-hashes", install.call_args.kwargs["extra_options"])
            self.assertIn("--only-binary=flashinfer-python,torch,triton,vllm,vllm-omni",
                          install.call_args.kwargs["extra_options"])
            with self.assertRaisesRegex(ValueError, "refresh"):
                build_image({**moss, "uv_packages": ["vllm==9.9.9"]}, offline=True)
            with self.assertRaisesRegex(ValueError, "Missing engine requirements"):
                build_image({**moss, "uv_requirements": "missing.txt"}, offline=True)

    def test_native_image_config_is_validated_before_any_cloud_build(self):
        spec = spec_for("models/001-moss-image")
        for override in (
            {"python_version": None}, {"python_version": "latest"},
            {"python_version": "3.12.1"}, {"uv_packages": []},
            {"uv_torch_backend": "auto"}, {"uv_torch_backend": "cu130 --pre"},
            {"uv_torch_backend": None}, {"image": "example/image:tag"},
        ):
            with self.subTest(override=override), self.assertRaises(ValueError):
                validate({**spec, **override})
        registry = spec_for("models/007-qwen38-pi")
        with self.assertRaises(ValueError):
            validate({**registry, "uv_torch_backend": "cu130"})
        missing_version = {k: v for k, v in spec.items() if k != "python_version"}
        with self.assertRaises(ValueError):
            validate(missing_version)

    def test_native_image_never_imports_registry(self):
        with (patch("runtime.serving.modal_setup.modal.Image.debian_slim") as native,
              patch("runtime.serving.modal_setup.modal.Image.from_registry") as registry):
            base = native.return_value
            installed = base.uv_pip_install.return_value
            cubin = installed.run_commands.return_value
            configured = cubin.run_commands.return_value
            result = runtime_image("debian_slim", env={"HF_HOME": "/data"},
                                   python_version="3.12", uv_torch_backend="cu130",
                                   uv_packages=("vllm==0.32.0", "vllm-omni==0.32.0"),
                                   commands=("flashinfer install-cubin-wheel", "flashinfer install-jit-cache-wheel --cuda-version 13.0 --mode minimal --sm sm120"))
            native.assert_called_once_with(python_version="3.12")
            registry.assert_not_called()
            base.uv_pip_install.assert_called_once_with(
                "vllm==0.32.0", "vllm-omni==0.32.0",
                extra_options="--torch-backend=cu130")
            installed.run_commands.assert_called_once_with("flashinfer install-cubin-wheel")
            cubin.run_commands.assert_called_once_with("flashinfer install-jit-cache-wheel --cuda-version 13.0 --mode minimal --sm sm120")
            configured.env.assert_called_once_with({"HF_HOME": "/data", "PYTHONPATH": "/opt"})
            self.assertIs(result, configured.env.return_value.add_local_dir.return_value)

    def test_uv_packages_are_validated_before_any_cloud_build(self):
        original = spec_for("models/005-minimax-h3-image-sglang")
        for invalid in (None, "sglang", [""], ["--upgrade"], [123], ["", "sglang"]):
            with self.subTest(value=invalid):
                candidate = copy.deepcopy(original)
                candidate["uv_packages"] = invalid
                with self.assertRaises(ValueError):
                    validate(candidate)

    def test_uv_install_only_applies_to_model_that_requests_it(self):
        with patch("runtime.serving.modal_setup.modal.Image.from_registry") as factory:
            image = factory.return_value.entrypoint.return_value
            result = runtime_image("example/image@sha256:" + "a" * 64, env={"HF_HOME": "/data"})
            image.uv_pip_install.assert_not_called()
            self.assertIs(result, image.env.return_value.add_local_dir.return_value)

        with patch("runtime.serving.modal_setup.modal.Image.from_registry") as factory:
            image = factory.return_value.entrypoint.return_value
            runtime_image("example/image@sha256:" + "b" * 64, env={},
                          uv_packages=("sglang[diffusion] @ git+https://github.com/sgl-project/sglang.git@abc#subdirectory=python",))
            image.env.assert_called_once_with({"SGLANG_BUILD_RUST_EXTS": "none"})
            build_env = image.env.return_value
            build_env.uv_pip_install.assert_called_once_with("sglang[diffusion] @ git+https://github.com/sgl-project/sglang.git@abc#subdirectory=python")
            build_env.uv_pip_install.return_value.env.assert_called_once()

    def test_native_wheel_only_install_uses_official_extra_index(self):
        wheel = "sglang[diffusion] @ https://example.com/sglang.whl"
        with (patch("runtime.serving.modal_setup.modal.Image.debian_slim") as native,
              patch("runtime.serving.modal_setup.modal.Image.from_registry") as registry):
            base = native.return_value
            runtime_image("debian_slim", env={}, python_version="3.12",
                          uv_packages=(wheel,), uv_torch_backend="cu130",
                          uv_extra_index_url="https://docs.sglang.ai/whl/cu130/",
                          uv_only_binary=True)
            base.env.assert_not_called()
            base.uv_pip_install.assert_called_once_with(
                wheel, extra_options="--torch-backend=cu130 --only-binary=:all:",
                extra_index_url="https://docs.sglang.ai/whl/cu130/")
            registry.assert_not_called()

    def test_wheel_policy_rejects_invalid_options(self):
        spec = spec_for("models/001-moss-image")
        for override in ({"uv_only_binary": "true"}, {"uv_only_binary": None},
                         {"uv_extra_index_url": "http://example.com"},
                         {"uv_extra_index_url": "https://user:secret@example.com"},
                         {"uv_extra_index_url": "https://example.com --pre"},
                         {"uv_extra_index_url": None}, {"uv_only_binary": []},
                         {"uv_only_binary": ["vllm", "vllm"]},
                         {"uv_only_binary": ["torch; echo bad"]},
                         {"uv_requirements": "../bad.txt"}, {"uv_requirements": "bad file.txt"}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                validate({**spec, **override})
        registry = spec_for("models/007-qwen38-pi")
        for override in ({"uv_only_binary": True}, {"uv_extra_index_url": "https://example.com/"}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                validate({**registry, **override})

    def test_model_runtime_forwards_image_options(self):
        with patch("runtime.serving.modal_setup.runtime_image") as factory:
            for directory in ("models/001-moss-image", "models/002-pika-h3-flex",
                              "models/005-minimax-h3-image-sglang", "models/006-minimax-h3-image-vllm"):
                with self.subTest(directory=directory):
                    spec = spec_for(directory)
                    build_image(spec, offline=True)
                    self.assertEqual(factory.call_args.args, ("debian_slim",))
                    kwargs = factory.call_args.kwargs
                    self.assertEqual(kwargs["python_version"], spec["python_version"])
                    self.assertEqual(kwargs["uv_torch_backend"], spec["uv_torch_backend"])
                    self.assertEqual(kwargs["uv_packages"], spec["uv_packages"])
                    self.assertEqual(kwargs["uv_only_binary"], spec.get("uv_only_binary", False))
                    self.assertEqual(kwargs["uv_extra_index_url"], spec.get("uv_extra_index_url"))
                    self.assertEqual(kwargs["commands"], spec["build_commands"])
                    self.assertEqual(kwargs["uv_requirements"], spec.get("uv_requirements"))
                    if "CUDA_HOME" in spec.get("env", {}):
                        self.assertEqual(kwargs["env"]["CUDA_HOME"], spec["env"]["CUDA_HOME"])


if __name__ == "__main__":
    unittest.main()
