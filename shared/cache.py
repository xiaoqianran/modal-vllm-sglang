"""Lightweight integrity checks for persisted model snapshots."""
from pathlib import Path
import stat


def file_inventory(snapshot):
    root = Path(snapshot)
    if not root.is_dir():
        raise RuntimeError(f"Materialized model is missing: {root}")
    inventory = {}
    for path in root.rglob("*"):
        # Follow HF file symlinks, but fetch each target's metadata only once.
        try:
            metadata = path.stat()
        except OSError as exc:
            raise RuntimeError(f"Broken or unreadable model file: {path}") from exc
        if not stat.S_ISREG(metadata.st_mode):
            if not path.is_symlink():
                continue
            raise RuntimeError(f"Broken or invalid model file: {path}")
        size = metadata.st_size
        if size <= 0:
            raise RuntimeError(f"Model snapshot contains empty files: {path}")
        inventory[path.relative_to(root).as_posix()] = size
    if not inventory:
        raise RuntimeError(f"Model files are empty: {root}")
    return dict(sorted(inventory.items()))
