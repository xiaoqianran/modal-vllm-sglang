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
    def test_all_three_engine_images_are_immutable_digest_references(self):
        for directory in ("models/001-moss-image", "models/002-pika-h3-flex", "models/005-minimax-h3-image"):
            with self.subTest(model=directory):
                spec = spec_for(directory)
                self.assertRegex(spec["image"], r"^[^\s@]+@sha256:[0-9a-f]{64}$")

    def test_minimax_uv_dependency_is_pinned_and_uses_existing_sglang_git_commit(self):
        spec = spec_for("models/005-minimax-h3-image")
        self.assertNotIn("uv_version", spec)
        self.assertEqual(len(spec["uv_packages"]), 1)
        self.assertIn("sglang[diffusion]", spec["uv_packages"][0])
        self.assertIn("a9d721f36e16dab1835de0203b620256da1e612d", spec["uv_packages"][0])
        self.assertNotIn("build_commands", spec)

    def test_pika_and_moss_keep_no_extra_dependency_installation(self):
        for directory in ("models/001-moss-image", "models/002-pika-h3-flex"):
            with self.subTest(model=directory):
                spec = spec_for(directory)
                self.assertFalse(spec.get("uv_packages"))
                self.assertFalse(spec.get("build_commands"))

    def test_uv_packages_are_validated_before_any_cloud_build(self):
        original = spec_for("models/005-minimax-h3-image")
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
                          uv_packages=("sglang[diffusion]==0.5.23",))
            image.env.assert_called_once_with({"SGLANG_BUILD_RUST_EXTS": "none"})
            build_env = image.env.return_value
            build_env.uv_pip_install.assert_called_once_with("sglang[diffusion]==0.5.23")
            build_env.uv_pip_install.return_value.env.assert_called_once()

    def test_minimax_uses_native_uv_layer_and_others_do_not(self):
        with patch("runtime.serving.modal_setup.runtime_image") as factory:
            build_image(spec_for("models/005-minimax-h3-image"), offline=True)
            kwargs = factory.call_args.kwargs
            self.assertEqual(len(kwargs["uv_packages"]), 1)
            self.assertNotIn("uv_version", kwargs)
            self.assertEqual(kwargs["commands"], ())

            for directory in ("models/001-moss-image", "models/002-pika-h3-flex"):
                build_image(spec_for(directory), offline=True)
                self.assertEqual(factory.call_args.kwargs["uv_packages"], ())


if __name__ == "__main__":
    unittest.main()
