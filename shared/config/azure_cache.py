"""Publish downloaded components using the inference cache contract."""
from pathlib import Path
import re
from shared.config.model_cache import component_specs, checked_inventory, verify_cache
from shared.tasks.batch import intent_digest
from shared.utils.files import atomic_json, read_json
from shared.config.storage import shared_storage, SHARED_MOUNT


def preparation_strategy(spec):
    """Select benchmark HF snapshots or specialized SGLang overlay materialization."""
    modes = {c.get("mode", "hf") for c in component_specs(spec).values()}
    if modes == {"hf"}:
        return "hf"
    if modes == {"sglang_overlay"}:
        return "sglang_overlay"
    raise ValueError("Mixed HF and SGLang Overlay components are unsupported")


def preserve_legacy_ready(root):
    """Keep the previous writable-volume manifest for a reversible shared rollout."""
    root = Path(root)
    marker = root / "model-ready.json"
    backup = root / "model-ready.legacy.json"
    if marker.is_file() and not backup.exists():
        old = read_json(marker)
        if ("manifest_version" in old or any(
                "path" in entry for entry in old.get("components", {}).values())):
            atomic_json(backup, old)


def publish_cache(spec, models, base, root, volume):
    root, base = Path(root), Path(base)
    expected_base = Path(SHARED_MOUNT) if shared_storage(spec) else root
    if not base.resolve().is_relative_to(expected_base.resolve()):
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
        key = "shared_path" if shared_storage(spec) else "path"
        reference_root = Path(SHARED_MOUNT) if shared_storage(spec) else root
        record["components"][name] = {key: path.relative_to(reference_root).as_posix(),
            "resolved_revision": model["revision"], "inventory": inventory}
    # Validate the candidate without replacing the last known-good marker.
    _, fingerprint = verify_cache(spec, root, phase="download_prepare", record=record)
    if shared_storage(spec):
        preserve_legacy_ready(root)
    atomic_json(root / "model-ready.json", record)
    volume.commit()
    return {"ready": True, "fingerprint": fingerprint}


def attach_shared_hf_snapshots(spec, volume, root=None, *, replace=False):
    """Adopt an existing verified HF native snapshot without copying weights."""
    from shared.config.model_cache import cache_root

    if not shared_storage(spec) or preparation_strategy(spec) != "hf":
        raise ValueError("Only shared Hugging Face models can adopt HF snapshots")
    root = Path(root) if root is not None else cache_root(spec)
    shared_root = Path(SHARED_MOUNT)
    marker = root / "model-ready.json"
    if marker.exists() and not replace:
        # Already-attached caches are safe to reuse, not silently overwritten.
        paths, fingerprint = verify_cache(spec, root, phase="shared_attach_reuse")
        return {"ready": True, "reused": True, "fingerprint": fingerprint,
                "paths": {name: str(path) for name, path in paths.items()}}
    record = {"version": 1, "cache_spec": intent_digest(component_specs(spec)),
              "components": {}}
    for name, config in component_specs(spec).items():
        repo, revision = config["model"], config["revision"]
        if not re.fullmatch(r"[a-f0-9]{40}", revision):
            raise ValueError("Attaching an HF snapshot requires a pinned 40-character revision")
        folder = "models--" + repo.replace("/", "--")
        snapshot = shared_root / folder / "snapshots" / revision
        if not snapshot.is_dir():
            raise RuntimeError(f"Shared HF snapshot is missing: {repo}@{revision}")
        # Symlink targets may live in the native HF blobs directory, but never outside the Volume.
        if not snapshot.resolve().is_relative_to(shared_root.resolve()):
            raise RuntimeError("Snapshot escapes the shared model Volume")
        inventory = checked_inventory(snapshot, config)
        for relative in inventory:
            file = (snapshot / relative).resolve()
            if not file.is_relative_to(shared_root.resolve()):
                raise RuntimeError("Snapshot contains a file outside the shared Volume")
        record["components"][name] = {
            "shared_path": snapshot.relative_to(shared_root).as_posix(),
            "resolved_revision": revision, "inventory": inventory,
        }
    _, fingerprint = verify_cache(spec, root, phase="shared_attach_prepare", record=record)
    preserve_legacy_ready(root)
    atomic_json(marker, record)
    volume.commit()
    return {"ready": True, "reused": False, "fingerprint": fingerprint}
