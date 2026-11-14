"""Reuse Modal Volume and code mounting without coupling model engines."""
from pathlib import Path

import modal

ROOT = Path(__file__).resolve().parent
VOLUME_NAME = "modal-forge"


def model_volume():
    """Preserve the existing volume and cloud cache paths."""
    return modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)


def runtime_image(registry, *, env, files=(), commands=()):
    """Mount lightweight Python sources after image configuration (copy=False)."""
    environment = dict(env)
    paths = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = ":".join(filter(None, (paths, "/opt")))
    image = modal.Image.from_registry(registry).entrypoint([])
    if commands:
        image = image.run_commands(*commands)
    image = image.env(environment)
    for local_file, remote_file in files:
        image = image.add_local_file(str(local_file), remote_file, copy=False)
    return image.add_local_dir(str(ROOT), "/opt/shared", copy=False)
