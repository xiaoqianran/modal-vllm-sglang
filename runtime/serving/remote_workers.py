"""Module-level CPU workers; receive the locally validated model configuration.

CPU cache workers intentionally do not import GPU request validators. They
verify the relevant cache contract; model-specific inference validation
remains at the local configuration boundary and in the GPU image.
"""
from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath


def publish_cache_entry(spec, manifest, base, root, strategy, volume_name, volume_version):
    """Publish and verify an inference-ready cache without serializing closures."""
    import modal
    from importlib import import_module
    from runtime.config.cache_publication import publish_cache
    from runtime.config.model_cache import component_specs, verify_cache

    volume = modal.Volume.from_name(volume_name, version=volume_version)
    volume.reload()
    if strategy == "sglang_overlay":
        implementation = import_module(spec["cache_implementation"])
        published = implementation.publish_cache(spec, manifest, base, root, volume)
    elif strategy == "hf":
        by_repo = {model["repo_id"]: model for model in manifest["models"]}
        models = {
            name: by_repo[item["model"]]
            for name, item in component_specs(spec).items()
        }
        published = publish_cache(spec, models, base, root, volume)
    else:
        raise ValueError(f"Unsupported cache publication strategy: {strategy}")
    paths, fingerprint = verify_cache(spec, phase="download_persisted")
    return {
        **published,
        "fingerprint": fingerprint,
        "paths": {name: str(path) for name, path in paths.items()},
    }


def cache_maintenance_entry(spec, migrate_only, volume_name, volume_version):
    """Inspect or migrate existing cache metadata on a CPU-only worker."""
    import modal
    from runtime.config.model_cache import inspect_or_migrate_cache

    volume = modal.Volume.from_name(volume_name, version=volume_version)
    volume.reload()
    return inspect_or_migrate_cache(spec, volume, migrate_only=migrate_only)


def attach_shared_hf_entry(spec, replace, volume_name, volume_version, sub_path):
    """Attach an already downloaded shared HF snapshot without copying weights."""
    import modal
    from runtime.config.cache_publication import attach_shared_hf_snapshots

    volume = modal.Volume.from_name(
        volume_name, version=volume_version
    ).with_mount_options(sub_path=sub_path)
    from runtime.config.model_cache import verify_cache
    result = attach_shared_hf_snapshots(spec, volume, replace=replace)
    paths, fingerprint = verify_cache(spec, phase="shared_attached")
    return {**result, "fingerprint": fingerprint,
            "paths": {key: str(path) for key, path in paths.items()}}


def check_shared_cache_entry(spec):
    """Verify a prepared cache from the mounted read-only/writable shared volume."""
    from runtime.config.model_cache import command_argv, verify_cache

    paths, fingerprint = verify_cache(spec, phase="fresh_cpu_start")
    return {
        "ready": True,
        "fingerprint": fingerprint,
        "paths": {name: str(value) for name, value in paths.items()},
        "command": command_argv(spec, paths),
    }


def probe_volume_file(relative_path):
    """Read-only integrity probe for a file below the mounted Volume root."""
    root = Path("/data").resolve()
    if not isinstance(relative_path, str) or not relative_path:
        raise ValueError("Invalid model path")
    relative = PurePosixPath(relative_path)
    if (relative.is_absolute() or ".." in relative.parts
            or relative.as_posix() != relative_path or "\\" in relative_path):
        raise ValueError("Invalid model path")
    target = root.joinpath(*relative.parts)
    resolved = target.resolve(strict=True)
    if not resolved.is_relative_to(root) or not target.is_file():
        raise ValueError("Model file escapes the shared Volume")
    size = resolved.stat().st_size
    if not size:
        raise RuntimeError("Model file is empty")
    digest = None
    if size <= 16 * 1024 * 1024:
        hasher = hashlib.sha256()
        with resolved.open("rb") as stream:
            for part in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(part)
        digest = hasher.hexdigest()
    return {
        "relative_path": relative_path,
        "bytes": size,
        "sha256": digest,
        "is_symlink": target.is_symlink(),
        "mount_read_only": True,
    }


def _legacy_cache_context():
    """Read canonical JSON configuration and reconstruct the mounted volume."""
    import json
    import os
    from runtime.serving.modal_setup import model_volume

    return json.loads(os.environ["MODEL_CACHE_SPEC_JSON"]), model_volume()


def legacy_prepare_cache_entry(refresh=False, migrate_only=False, repair=False):
    """Compatibility CPU preparation worker; preserve the existing RPC signature."""
    from runtime.config.model_cache import prepare_cache

    spec, volume = _legacy_cache_context()
    if repair and not refresh and not migrate_only:
        try:
            return prepare_cache(spec, volume)
        except RuntimeError:
            return prepare_cache(spec, volume, refresh=True)
    return prepare_cache(spec, volume, refresh=refresh, migrate_only=migrate_only)


def legacy_check_cache_entry():
    """Compatibility cache-only check; it never downloads model weights."""
    from runtime.config.model_cache import verify_cache

    spec, _ = _legacy_cache_context()
    _, fingerprint = verify_cache(spec)
    return {"ready": True, "fingerprint": fingerprint}


def provision_info_entry():
    """Report deployment metadata from the remote image environment."""
    import os
    return os.environ["MODEL_DEPLOY_VERSION"]
