"""Reuse Modal Volume and code mounting without coupling model engines."""
from pathlib import Path
import re

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
                  uv_extra_index_url=None, uv_only_binary=False, uv_requirements=None):
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
            packages = ":all:" if uv_only_binary is True else ",".join(sorted(uv_only_binary))
            flags.append(f"--only-binary={packages}")
        if uv_requirements:
            if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*\.txt", uv_requirements):
                raise ValueError("Invalid engine requirements filename")
            lock = ROOT / "serving" / "locks" / uv_requirements
            if not lock.is_file():
                raise ValueError(f"Missing engine requirements: {uv_requirements}")
            # Reject stale locks when the model's declared engine pins change.
            locked = {re.sub(r"\[[^\]]*\]", "", line.rstrip(" \\"))
                      for line in lock.read_text(encoding="utf-8").splitlines()
                      if line and not line[0].isspace() and not line.startswith(("#", "-"))}
            for package in uv_packages:
                if re.sub(r"\[[^\]]*\]", "", package) not in locked:
                    raise ValueError(f"Engine requirements do not match {package}; refresh {uv_requirements}")
            flags.append("--require-hashes")
        options = {"extra_options": " ".join(flags)} if flags else {}
        if uv_extra_index_url:
            options["extra_index_url"] = uv_extra_index_url
        if uv_requirements:
            options["requirements"] = [str(lock)]
            options["index_url"] = "https://pypi.org/simple"
            options["uv_version"] = "0.12.5"
            image = image.uv_pip_install(**options)
        else:
            image = image.uv_pip_install(*uv_packages, **options)
    # Separate shared kernel setup from model-specific system packages.
    for command in commands:
        image = image.run_commands(command)
    image = image.env(environment)
    for local_file, remote_file in files:
        image = image.add_local_file(str(local_file), remote_file, copy=False)
    return image.add_local_dir(str(ROOT), "/opt/runtime", copy=False, ignore=['**/__pycache__/**', '**/*.pyc', '**/*.pyo'])
