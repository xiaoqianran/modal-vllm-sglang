"""Pika CPU materialization image policies; defines images but never builds them."""
import unittest
from importlib import import_module
from pathlib import Path
from unittest.mock import patch

from runtime.config.model_spec import validate
_materializer = import_module("models.002-pika-h3-flex.model_impl.materializer_image")
SGLANG_MATERIALIZER = _materializer.SGLANG_MATERIALIZER
build_pika_materializer_image = _materializer.build_pika_materializer_image
from runtime.utils.files import read_json

ROOT = Path(__file__).resolve().parents[1]


class PikaMaterializerImageTests(unittest.TestCase):
    def setUp(self):
        self.spec = validate(read_json(ROOT / "models/002-pika-h3-flex/model.json"))

    def test_slim_uv_does_not_pull_gpu_registry_image(self):
        with (
            patch.object(_materializer.modal.Image, "debian_slim") as slim,
            patch.object(_materializer.modal.Image, "from_registry") as registry,
        ):
            image = slim.return_value.apt_install.return_value
            uv = image.uv_pip_install.return_value
            output = build_pika_materializer_image(self.spec, modal_version="1.6.4")
        slim.assert_called_once_with(python_version="3.12")
        image.uv_pip_install.assert_called_once_with(
            "modal[api-proxy-support]==1.6.4", SGLANG_MATERIALIZER,
            "huggingface_hub>=0.30,<2",
        )
        uv.env.assert_called_once()
        self.assertEqual(uv.env.call_args.args[0]["HF_HUB_OFFLINE"], "1")
        first_mount = uv.env.return_value.add_local_dir
        first_mount.assert_called_once()
        self.assertEqual(first_mount.call_args.args[1], "/opt/models/002-pika-h3-flex/model_impl")
        self.assertEqual(uv.env.call_args.args[0]["PYTHONPATH"], "/opt")
        self.assertIs(output, first_mount.return_value.add_local_dir.return_value)
        registry.assert_not_called()

    def test_only_pika_overlay_uses_materializer(self):
        with self.assertRaisesRegex(ValueError, "Only Pika"):
            build_pika_materializer_image(
                validate(read_json(ROOT / "models/001-moss-image/model.json")),
                modal_version="1.6.4",
            )


if __name__ == "__main__":
    unittest.main()
