"""Publish downloaded components using the inference cache contract."""
from pathlib import Path
import re
from shared.config.model_cache import component_specs, checked_inventory, verify_cache
from shared.tasks.batch import intent_digest
from shared.utils.files import atomic_json


def preparation_strategy(spec):
    """Select benchmark HF snapshots or specialized SGLang overlay materialization."""
    modes = {c.get("mode", "hf") for c in component_specs(spec).values()}
    if modes == {"hf"}:
        return "hf"
    if modes == {"sglang_overlay"}:
        return "sglang_overlay"
    raise ValueError("Mixed HF and SGLang Overlay components are unsupported")


def publish_cache(spec, models, base, root, volume):
    root, base = Path(root), Path(base)
    if not base.resolve().is_relative_to(root.resolve()):
        raise ValueError("Downloaded components must be inside the service cache")
    record = {"version": 1, "cache_spec": intent_digest(component_specs(spec)), "components": {}}
    for name, config in component_specs(spec).items():
        model = models[name]
        if model["repo_id"] != config["model"]:
            raise ValueError("Component repository mismatch")
        if not re.fullmatch(r"[a-f0-9]{40}", model["revision"]):
            raise ValueError("Component revision must be pinned")
        if re.fullmatch(r"[a-f0-9]{40}", config["revision"]) and config["revision"] != model["revision"]:
            raise ValueError("Component revision mismatch")
        path = base / model["repo_id"] / model["revision"] / "model"
        if not path.resolve().is_relative_to(base.resolve()):
            raise ValueError("Component path escapes download directory")
        inventory = checked_inventory(path, config)
        expected = {key: value["size"] for key, value in model["files"].items()}
        if inventory != expected:
            raise RuntimeError(f"Incomplete downloaded component: {name}")
        record["components"][name] = {"path": path.relative_to(root).as_posix(),
            "resolved_revision": model["revision"], "inventory": inventory}
    # Validate the candidate without replacing the last known-good marker.
    _, fingerprint = verify_cache(spec, root, phase="download_prepare", record=record)
    atomic_json(root / "model-ready.json", record)
    volume.commit()
    return {"ready": True, "fingerprint": fingerprint}
