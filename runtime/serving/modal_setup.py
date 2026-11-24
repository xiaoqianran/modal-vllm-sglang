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


def runtime_image(base, *, env, files=(), commands=(), uv_packages=(),
                  python_version=None, uv_torch_backend=None,
                  uv_extra_index_url=None, uv_only_binary=False):
    """Prefer native Modal images; registry images remain an explicit alternative."""
    environment = dict(env)
    paths = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = ":".join(filter(None, (paths, "/opt")))
    if base == "debian_slim":
        image = modal.Image.debian_slim(python_version=python_version)
    else:
        image = modal.Image.from_registry(base).entrypoint([])
    if uv_packages:
        # SGLang's optional Rust extensions are not needed by the image
        # diffusion runtime. The build flag must precede uv_pip_install:
        # otherwise building the pinned Git source can require Cargo.
        if any("sglang[diffusion]" in package and "git+" in package
               for package in uv_packages):
            image = image.env({"SGLANG_BUILD_RUST_EXTS": "none"})
        # Explicit wheel/index policies preserve the same dependency layer
        # across services and reject an unexpected source build.
        flags = []
        if uv_torch_backend:
            flags.append(f"--torch-backend={uv_torch_backend}")
        if uv_only_binary:
            flags.append("--only-binary=:all:")
        options = {"extra_options": " ".join(flags)} if flags else {}
        if uv_extra_index_url:
            options["extra_index_url"] = uv_extra_index_url
        image = image.uv_pip_install(*uv_packages, **options)
    if commands:
        image = image.run_commands(*commands)
    image = image.env(environment)
    for local_file, remote_file in files:
        image = image.add_local_file(str(local_file), remote_file, copy=False)
    return image.add_local_dir(str(ROOT), "/opt/runtime", copy=False, ignore=['**/__pycache__/**', '**/*.pyc', '**/*.pyo'])
