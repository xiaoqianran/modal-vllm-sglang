"""Single-writer state and verified, atomically published artifacts."""
from pathlib import Path

from shared.utils.files import sha256_file


from shared.utils.files import atomic_json as save, atomic_write as write, file_lock as lock


from shared.utils.files import local_path
from shared.utils.files import read_json


def layout(directory):
    marker = Path(directory) / "layout.json"
    return read_json(marker) if marker.exists() else {}


def media_directory(directory):
    return Path(layout(directory).get("media", directory))


def telemetry_directory(directory):
    return Path(layout(directory).get("logs", Path(directory).parent.parent))


def output_stem(directory, job, plan):
    if layout(directory):
        return f"{job['item']}_{job['step']}"
    kind = plan["services"][job["uses"].split(".")[0]]["spec"]["kind"]
    siblings = sum(other["item"] == job["item"] and
                   plan["services"][other["uses"].split(".")[0]]["spec"]["kind"] == kind
                   for other in plan["jobs"])
    return job["item"] if siblings == 1 else f"{job['item']}-{job['step']}"


def runtime_root(output):
    return Path(output).resolve().parent / ".runtime"


def resolve_task(root, value):
    candidate = local_path(root, value)
    if (candidate / "plan.json").exists():
        return candidate
    tasks = Path(root) / ".runtime/tasks" / candidate.name
    scenes = sorted(tasks.glob("*/plan.json"))
    if len(scenes) == 1:
        return scenes[0].parent
    raise ValueError("Task not found or has multiple scenes; specify its .runtime/tasks path")


def record_event(directory, job, changes):
    """Local-only diagnostics; failure cannot change generation state."""
    import json
    from datetime import datetime, timezone
    config = layout(directory)
    if not config:
        return
    try:
        folder = Path(config["logs"]) / "tasks"
        path = folder / (Path(directory).parent.name + "_" + Path(directory).name + ".jsonl")
        with lock(folder, timeout=2):
            if path.exists() and path.stat().st_size > 8 * 1024 * 1024:
                path.replace(path.with_suffix(".previous.jsonl"))
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "job": job,
                                         "changes": changes}, ensure_ascii=False) + "\n")
    except (OSError, RuntimeError, ValueError):
        pass

from shared.utils.media import image_format


def publish(root, folder, name, data, *, filename=None, refuse_existing=False):
    if name == "image":
        suffix, mime = image_format(data)
    elif name == "video" and len(data) >= 32 and data[4:8] == b"ftyp":
        suffix, mime = ".mp4", "video/mp4"
    else:
        raise ValueError("Invalid output format")
    if filename is not None:
        # Public outputs are flat; per-step manifests live in a separate folder.
        if (not isinstance(filename, str) or not filename.isascii()
                or not filename.replace("-", "").replace("_", "").isalnum()):
            raise ValueError("Invalid output filename")
    relative = f"{folder}/{filename or name}{suffix}" if folder else f"{filename or name}{suffix}"
    path = local_path(root, relative)
    if refuse_existing and path.exists():
        raise FileExistsError(f"Output already exists, refusing overwrite: {path}")
    write(path, data)
    return {"type": name, "path": path.relative_to(Path(root).resolve()).as_posix(),
            "size": len(data), "sha256": sha256_file(path), "mime": mime}


def valid(root, artifact):
    try:
        path = local_path(root, artifact["path"])
        return (path.is_file() and path.stat().st_size == artifact["size"]
                and sha256_file(path) == artifact["sha256"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
