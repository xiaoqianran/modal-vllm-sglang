"""Local output layout shared by independent model entrypoints."""
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from shared.files import atomic_json, sha256_file

REPO = Path(__file__).resolve().parent.parent


def component(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
        raise ValueError(f"Invalid output path component: {value!r}")
    if value.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}:
        raise ValueError("Reserved filename")
    return value


def new_id():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]


def run_dir(task, run_id=None, *, scene="001-main", root=None, benchmark=False):
    base = Path(root) if root is not None else REPO / ("benchmarks" if benchmark else "outputs")
    # Inference products are organized by project / scene. Benchmark runs remain separate.
    path = base / component(task) / component(run_id or new_id()) if benchmark else base / component(task) / component(scene)
    path.mkdir(parents=True, exist_ok=True)
    return path


def step_dir(run, item, step):
    path = Path(run) / ".runner" / "items" / component(item) / component(step)
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
