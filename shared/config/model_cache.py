"""CPU preparation and offline verification for HF snapshots and SGLang overlays."""
from pathlib import Path, PurePosixPath
import json
import time

from shared.tasks.batch import intent_digest
from shared.config.cache import file_inventory
from shared.utils.files import atomic_json, read_json
from shared.config.storage import shared_storage, SHARED_MOUNT

DOWNLOAD_MAX_WORKERS = 8


def cache_root(spec):
    return Path(cache_directory(spec))


def cache_directory(spec):
    """Container path, independent of the SDK host's operating system."""
    return spec.get("cache_directory", "/mnt/modal/models/" + spec["service"])


def migrate_legacy_cache(spec, volume, root):
    """CPU-only manifest conversion in place; never copy or download weights."""
    marker = root / "model-ready.json"
    old = read_json(marker)
    expected_version = 2 if spec["service"] == "pika" else 1
    if old.get("manifest_version") != expected_version:
        raise RuntimeError("Unsupported legacy cache manifest")
    record = {"version": 1, "cache_spec": intent_digest(component_specs(spec)), "components": {}}
    for name, config in component_specs(spec).items():
        entry = old.get("image" if name == "model" else name, {}) if spec["service"] == "minimax" else old
        if entry.get("repo_id") != config["model"]:
            raise RuntimeError("Legacy cache model identity mismatch")
        if config.get("mode") == "sglang_overlay":
            if config["revision"] != "main":
                raise RuntimeError("Legacy overlay does not attest a pinned revision; prepare with --refresh")
            path = Path(entry.get("materialized_path", ""))
            if not path.is_absolute() or path.parent.resolve() != (root / "sglang/materialized_models").resolve():
                raise RuntimeError("Invalid legacy overlay path")
        else:
            relative = PurePosixPath(entry.get("snapshot_relative", ""))
            repo_folder = "models--" + config["model"].replace("/", "--")
            if (relative.is_absolute() or len(relative.parts) != 3
                    or relative.parts[:2] != (repo_folder, "snapshots") or ".." in relative.parts):
                raise RuntimeError("Invalid legacy snapshot path")
            path = root / "hub" / relative.as_posix()
            if config["revision"] != "main" and path.name != config["revision"]:
                raise RuntimeError("Legacy cache revision mismatch")
            if spec["service"] == "minimax" and entry.get("revision") != config["revision"]:
                raise RuntimeError("Legacy component revision mismatch")
        if not path.resolve().is_relative_to(root.resolve()):
            raise RuntimeError("Legacy cache path escapes root")
        inventory = checked_inventory(path, config)
        if inventory != entry.get("inventory"):
            raise RuntimeError("Legacy cache changed or incomplete")
        record["components"][name] = {"path": path.resolve().relative_to(root.resolve()).as_posix(),
                                      "resolved_revision": path.name, "inventory": inventory}
    # Preserve the old manifest for rollback. Publish only after every component passed.
    atomic_json(root / "model-ready.legacy.json", old)
    atomic_json(marker, record)
    verify_cache(spec, root, phase="cpu_prepare")
    volume.commit()
    return {"ready": True, "migrated": True, "fingerprint": intent_digest(record)}


def component_specs(spec):
    return {"model": {"model": spec["model"], "revision": spec["revision"],
                      **spec.get("cache", {})}, **spec.get("components", {})}


def checked_inventory(path, config):
    inventory = file_inventory(path)
    if not any(Path(name).suffix in {".safetensors", ".bin", ".pt", ".pth"} for name in inventory):
        raise RuntimeError(f"No model weights: {path}")
    for pattern in config.get("required_files", []):
        if not any(path.glob(pattern)):
            raise RuntimeError(f"Missing required model component: {pattern}")
    return inventory


def verify_cache(spec, root=None, *, phase=None, record=None):
    """Full file-name/size validation; optional timing never changes checks."""
    started = time.perf_counter()
    success = False
    try:
        result = _verify_cache_full(spec, root, record=record)
        success = True
        return result
    finally:
        if phase is not None:
            print(json.dumps({"event": "cache_verified", "service": spec["service"],
                              "phase": phase, "validation": "full_inventory",
                              "success": success,
                              "seconds": time.perf_counter() - started}), flush=True)


def _verify_cache_full(spec, root=None, *, record=None):
    root = Path(root) if root is not None else cache_root(spec)
    if record is None:
        marker = root / "model-ready.json"
        if not marker.is_file():
            raise RuntimeError("CPU cache missing; run download_model.py first")
        record = read_json(marker)
    if record.get("version") != 1 or record.get("cache_spec") != intent_digest(component_specs(spec)):
        raise RuntimeError("Model cache configuration mismatch; prepare it again on CPU")
    paths = {}
    for name, config in component_specs(spec).items():
        entry = record.get("components", {}).get(name, {})
        if shared_storage(spec):
            relative = entry.get("shared_path", "")
            shared = Path(SHARED_MOUNT)
            if not isinstance(relative, str) or not relative:
                raise RuntimeError("Invalid shared model path")
            rel_path = PurePosixPath(relative)
            path = shared / relative
            resolved = path.resolve()
            if (rel_path.is_absolute() or ".." in rel_path.parts
                    or rel_path.as_posix() != relative
                    or "\\" in relative or not resolved.is_relative_to(shared.resolve())
                    or resolved == shared.resolve() or "path" in entry):
                raise RuntimeError("Invalid shared model path")
        else:
            relative = entry.get("path", "")
            path = (root / relative).resolve()
            if (not relative or not path.is_relative_to(root.resolve()) or path == root.resolve()
                    or "shared_path" in entry):
                raise RuntimeError("Invalid model cache path")
        if checked_inventory(path, config) != entry.get("inventory"):
            raise RuntimeError(f"Model cache changed or incomplete: {name}")
        paths[name] = path
    return paths, intent_digest(record)


def prepare_cache(spec, volume, root=None, *, refresh=False, migrate_only=False):
    """Only called by the CPU downloader; a GPU startup never calls this."""
    root = Path(root) if root is not None else cache_root(spec)
    if shared_storage(spec):
        if refresh or migrate_only:
            raise RuntimeError("Shared models must be prepared by scripts/download_models.py")
        paths, fingerprint = verify_cache(spec, root, phase="cpu_prepare")
        return {"ready": True, "fingerprint": fingerprint, "paths": {k: str(v) for k, v in paths.items()}}
    if refresh and migrate_only:
        raise ValueError("--refresh and --migrate-only are mutually exclusive")
    if migrate_only and not (root / "model-ready.json").is_file():
        raise RuntimeError("No existing cache manifest to migrate; no weights were downloaded")
    # Reuse the immutable prepared cache, including resolved commits for branch refs.
    if (root / "model-ready.json").exists() and not refresh:
        if "manifest_version" in read_json(root / "model-ready.json"):
            return migrate_legacy_cache(spec, volume, root)
        paths, fingerprint = verify_cache(spec, root, phase="cpu_prepare")
        return {"ready": True, "fingerprint": fingerprint}
    from huggingface_hub import snapshot_download
    record = {"version": 1, "cache_spec": intent_digest(component_specs(spec)), "components": {}}
    for name, config in component_specs(spec).items():
        if config.get("mode", "hf") == "sglang_overlay":
            from sglang.multimodal_gen.runtime.utils import hf_diffusers_utils
            # 保留 SGLang Overlay 物化，仅控制其调用的 HF 快照文件线程数。
            from unittest.mock import patch
            original = hf_diffusers_utils.snapshot_download
            def limited_snapshot_download(*args, **kwargs):
                kwargs.setdefault("max_workers", DOWNLOAD_MAX_WORKERS)
                return original(*args, **kwargs)
            with patch.object(hf_diffusers_utils, "snapshot_download", limited_snapshot_download):
                path = Path(hf_diffusers_utils.maybe_download_model(
                    config["model"], force_diffusers_model=True, revision=config["revision"]))
            if not path.resolve().is_relative_to(root.resolve()):
                raise RuntimeError("Overlay must be materialized inside this model's persisted cache")
            resolved = path.name
        else:
            path = Path(snapshot_download(config["model"], revision=config["revision"],
                                         allow_patterns=config.get("allow_patterns"), cache_dir=str(root / "hub"),
                                         force_download=refresh, max_workers=DOWNLOAD_MAX_WORKERS))
            resolved = path.name
        record["components"][name] = {"path": path.resolve().relative_to(root.resolve()).as_posix(),
                                      "resolved_revision": resolved,
                                      "inventory": checked_inventory(path, config)}
    atomic_json(root / "model-ready.json", record)
    verify_cache(spec, root, phase="cpu_prepare")
    volume.commit()
    return {"ready": True, "fingerprint": intent_digest(record)}


def command_argv(spec, paths):
    values = {"model": spec["model"], "port": str(spec.get("port", 8000)),
              "model_path": str(paths["model"]),
              **{name + "_path": str(path) for name, path in paths.items()}}
    argv = []
    for arg in spec["command"]:
        for key, value in values.items():
            arg = arg.replace("{" + key + "}", value)
        if "{" in arg or "}" in arg:
            try:
                structured = json.loads(arg)
            except (ValueError, TypeError):
                raise ValueError(f"Unknown command placeholder: {arg}") from None
            if not isinstance(structured, dict):
                raise ValueError(f"Unknown command placeholder: {arg}")
        argv.append(arg)
    return argv
