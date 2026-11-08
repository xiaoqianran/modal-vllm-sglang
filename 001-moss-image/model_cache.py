"""Read-only MOSS CPU-prepared Hugging Face cache validation."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.cache import file_inventory

MANIFEST_VERSION = 1
DEFAULT_MODEL = "OpenMOSS-Team/MOSS-Image-SFT"


def cache_root():
    return Path(os.environ.get("HF_HOME", "/mnt/modal/007-moss-image"))


def get_ready_model(repo_id=DEFAULT_MODEL):
    """GPU may only use an existing, verified CPU snapshot; never download."""
    root = cache_root()
    marker = root / "model-ready.json"
    if not marker.is_file():
        raise RuntimeError("MOSS CPU cache missing. Run: uv run modal run download_model.py")
    manifest = json.loads(marker.read_text(encoding="utf-8"))
    if manifest.get("manifest_version") != MANIFEST_VERSION or manifest.get("repo_id") != repo_id:
        raise RuntimeError("MOSS cache manifest mismatch; rerun the CPU downloader")
    # The same Volume has different mount paths in CPU and GPU containers.
    # Store the Hugging Face snapshot location *relative* to HF_HOME/hub.
    relative = Path(manifest.get("snapshot_relative", ""))
    repo_folder = "models--" + repo_id.replace("/", "--")
    if (relative.is_absolute() or len(relative.parts) != 3
            or relative.parts[:2] != (repo_folder, "snapshots")
            or relative.parts[2] in ("", ".", "..")):
        raise RuntimeError("MOSS cache snapshot path is invalid")
    path = root / "hub" / relative
    if path.parent.resolve() != (root / "hub" / repo_folder / "snapshots").resolve():
        raise RuntimeError("MOSS cache snapshot is outside its persisted model directory")
    inventory = file_inventory(path)
    for key in ("file_count", "total_bytes", "inventory"):
        actual = len(inventory) if key == "file_count" else (
            sum(inventory.values()) if key == "total_bytes" else inventory)
        if actual != manifest.get(key):
            raise RuntimeError(f"MOSS cache snapshot changed or incomplete: {key}")
    if not any(name.lower().endswith((".safetensors", ".bin", ".pt")) for name in inventory):
        raise RuntimeError("MOSS cache has no model weights")
    return repo_id
