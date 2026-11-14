"""CPU preparation and offline verification for HF snapshots and SGLang overlays."""
from pathlib import Path

from shared.batch import intent_digest
from shared.cache import file_inventory
from shared.files import atomic_json, read_json


def cache_root(spec):
    return Path("/mnt/modal/models") / spec["service"]


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
    from huggingface_hub import snapshot_download

    root = Path(root) if root is not None else cache_root(spec)
    # Reuse the immutable prepared cache, including resolved commits for branch refs.
    if (root / "model-ready.json").exists() and not refresh:
        paths, fingerprint = verify_cache(spec, root)
        return {"ready": True, "fingerprint": fingerprint}
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
