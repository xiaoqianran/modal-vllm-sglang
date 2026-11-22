"""Local output layout shared by independent model entrypoints."""
import re
from pathlib import Path

from runtime.utils.files import atomic_json, sha256_file

REPO = Path(__file__).resolve().parents[2]


def validate_path_component(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
        raise ValueError(f"Invalid output path component: {value!r}")
    if value.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}:
        raise ValueError("Reserved filename")
    return value


def run_dir(project, *, scene="001-main", root=None):
    base = Path(root) if root is not None else REPO / "outputs"
    path = base / validate_path_component(project) / validate_path_component(scene)
    path.mkdir(parents=True, exist_ok=True)
    return path


def step_dir(run, item, step):
    path = Path(run) / ".runner" / "items" / validate_path_component(item) / validate_path_component(step)
    path.mkdir(parents=True, exist_ok=True)
    return path


def record_artifact(run, path, kind, *, source=None):
    run, path = Path(run).resolve(), Path(path).resolve()
    artifact = {"type": kind, "path": path.relative_to(run).as_posix(),
                "size": path.stat().st_size, "sha256": sha256_file(path)}
    if source:
        artifact["source"] = source
    metadata = step_dir(run, path.stem, kind) / "artifacts.json"
    atomic_json(metadata, {"artifacts": {kind: artifact}})
    return artifact


# Compatibility alias for model entrypoints created before this refactor.
component = validate_path_component
