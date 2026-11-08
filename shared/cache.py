"""Lightweight integrity checks for persisted model snapshots."""
from pathlib import Path


def file_inventory(snapshot):
    root = Path(snapshot)
    if not root.is_dir():
        raise RuntimeError(f"Materialized model is missing: {root}")
    inventory = {}
    for path in sorted(root.rglob("*")):
        if not (path.is_file() or path.is_symlink()):
            continue
        if not path.is_file():
            raise RuntimeError(f"Broken or invalid model file: {path}")
        size = path.stat().st_size
        if size <= 0:
            raise RuntimeError(f"Model snapshot contains empty files: {path}")
        inventory[path.relative_to(root).as_posix()] = size
    if not inventory:
        raise RuntimeError(f"Model files are empty: {root}")
    return inventory
