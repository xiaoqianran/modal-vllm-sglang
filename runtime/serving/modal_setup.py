"""Reuse Modal Volume and code mounting without coupling model engines."""
from pathlib import Path

import modal
from runtime.config.storage import VOLUME_MOUNT, VOLUME_VERSION, VOLUME_SUB_PATH, runtime_volume_name

ROOT = Path(__file__).resolve().parents[1]


def model_volume():
    """Project storage shared by model workers and SDK clients."""
    return modal.Volume.from_name(runtime_volume_name(), create_if_missing=True, version=VOLUME_VERSION)


def model_mounts(spec):
    """All services mount the same writable V2 Volume once."""
    return {VOLUME_MOUNT: model_volume().with_mount_options(sub_path=VOLUME_SUB_PATH)}


def runtime_image(registry, *, env, files=(), commands=(), uv_packages=()):
    """Mount lightweight Python sources after image configuration (copy=False)."""
    environment = dict(env)
    paths = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = ":".join(filter(None, (paths, "/opt")))
    image = modal.Image.from_registry(registry).entrypoint([])
    if uv_packages:
        # Modal provides uv automatically and targets
        # the first Python on PATH (the existing SGLang image environment).
        image = image.uv_pip_install(*uv_packages)
    if commands:
        image = image.run_commands(*commands)
    image = image.env(environment)
    for local_file, remote_file in files:
        image = image.add_local_file(str(local_file), remote_file, copy=False)
    return image.add_local_dir(str(ROOT), "/opt/runtime", copy=False)
