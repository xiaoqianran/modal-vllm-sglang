"""CPU preparation and offline verification for HF snapshots and SGLang overlays."""
from pathlib import Path, PurePosixPath

from shared.batch import intent_digest
from shared.cache import file_inventory
from shared.files import atomic_json, read_json


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
        record["components"][name] = {"path": path.relative_to(root).as_posix(),
                                      "resolved_revision": path.name, "inventory": inventory}
    # Preserve the old manifest for rollback. Publish only after every component passed.
    atomic_json(root / "model-ready.legacy.json", old)
    atomic_json(marker, record)
    verify_cache(spec, root)
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


def verify_cache(spec, root=None):
    root = Path(root) if root is not None else cache_root(spec)
    marker = root / "model-ready.json"
    if not marker.is_file():
        raise RuntimeError("CPU cache missing; run download_model.py first")
    record = read_json(marker)
    if record.get("version") != 1 or record.get("cache_spec") != intent_digest(component_specs(spec)):
        raise RuntimeError("Model cache configuration mismatch; prepare it again on CPU")
    paths = {}
    for name, config in component_specs(spec).items():
        entry = record.get("components", {}).get(name, {})
        relative = entry.get("path", "")
        path = (root / relative).resolve()
        if not relative or not path.is_relative_to(root.resolve()) or path == root.resolve():
            raise RuntimeError("Invalid model cache path")
        if checked_inventory(path, config) != entry.get("inventory"):
            raise RuntimeError(f"Model cache changed or incomplete: {name}")
        paths[name] = path
    return paths, intent_digest(record)


def prepare_cache(spec, volume, root=None, *, refresh=False):
    """Only called by the CPU downloader; a GPU startup never calls this."""
    root = Path(root) if root is not None else cache_root(spec)
    # Reuse the immutable prepared cache, including resolved commits for branch refs.
    if (root / "model-ready.json").exists() and not refresh:
        if "manifest_version" in read_json(root / "model-ready.json"):
            return migrate_legacy_cache(spec, volume, root)
        paths, fingerprint = verify_cache(spec, root)
        return {"ready": True, "fingerprint": fingerprint}
    from huggingface_hub import snapshot_download
    record = {"version": 1, "cache_spec": intent_digest(component_specs(spec)), "components": {}}
    for name, config in component_specs(spec).items():
        if config.get("mode", "hf") == "sglang_overlay":
            from sglang.multimodal_gen.runtime.utils.hf_diffusers_utils import maybe_download_model
            path = Path(maybe_download_model(config["model"], force_diffusers_model=True))
            if not path.resolve().is_relative_to(root.resolve()):
                raise RuntimeError("Overlay must be materialized inside this model's persisted cache")
            resolved = path.name
        else:
            path = Path(snapshot_download(config["model"], revision=config["revision"],
                                         allow_patterns=config.get("allow_patterns"), cache_dir=str(root / "hub"),
                                         force_download=refresh))
            resolved = path.name
        record["components"][name] = {"path": path.relative_to(root).as_posix(),
                                      "resolved_revision": resolved,
                                      "inventory": checked_inventory(path, config)}
    atomic_json(root / "model-ready.json", record)
    verify_cache(spec, root)
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
            raise ValueError(f"Unknown command placeholder: {arg}")
        argv.append(arg)
    return argv
