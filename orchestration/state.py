"""Single-writer state and verified, atomically published artifacts."""
from pathlib import Path

from runtime.utils.files import sha256_file


from runtime.utils.files import atomic_json as save, atomic_write as write, file_lock as lock


from runtime.utils.files import local_path
from runtime.utils.files import read_json


def file_stamp(path):
    """Cheap change detection; a missing file is a distinct snapshot."""
    try:
        stat = Path(path).stat()
        return stat.st_mtime_ns, stat.st_size
    except FileNotFoundError:
        return None


def needs_local_delivery(record, *, directory=None):
    """A receipt alone is not evidence that the local file still exists."""
    artifact = record.get("local_artifact")
    if not artifact or record.get("local_recheck_epoch", 0) > record.get("local_verified_epoch", 0):
        return True
    # The scheduler's history scan is state-only. Actual collection and explicit
    # resume check the disk; legacy receipts acquire a baseline on first use.
    if directory is None:
        return False
    try:
        path = local_path(media_directory(directory), artifact["path"])
        stamp = local_media_stamp(path)
        return stamp is None or stamp != artifact.get("file_stamp") or stamp[2] != artifact.get("size")
    except (OSError, KeyError, TypeError, ValueError):
        return True


def local_media_stamp(path):
    """Cheap change detector; never a substitute for SHA-256 on revalidation."""
    import stat

    try:
        info = Path(path).stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    # JSON-compatible list; ctime/inode also help detect file replacement.
    return [info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_ino]


def pending_items(plan, current):
    """Only schedule work whose generation, delivery or terminal proof is unfinished."""
    from runtime.delivery import destinations
    required = destinations(plan)
    items = {}
    for job in plan["jobs"]:
        item = job["item"]
        record = current["jobs"][job["key"]]
        pending = record["status"] not in {"completed", "failed"}
        if record["status"] == "completed":
            if "local" in required and needs_local_delivery(record):
                pending = True
            if "huggingface" in required:
                archive = record.get("cloud_archive") or {}
                pending = pending or not archive or archive.get("derivative_status") == "pending"
        direct = current.get("direct_runs", {}).get(item)
        if direct and not direct.get("terminal_published"):
            pending = True
        cloud = current.get("cloud_runs", {}).get(item)
        if cloud and (cloud.get("status") not in {"completed", "failed"} or cloud.get("recovery_requested")):
            pending = True
        items[item] = items.get(item, False) or pending
    pending = [item for item, unfinished in items.items() if unfinished]
    if not pending and items and current.get("registered_at") and not current.get("finished_at"):
        # Finalize the durable report once, even after a worker interruption.
        pending.append(next(iter(items)))
    return pending


def layout(directory):
    marker = Path(directory) / "layout.json"
    return read_json(marker) if marker.exists() else {}


def media_directory(directory):
    return Path(layout(directory).get("media", directory))


def telemetry_directory(directory):
    return Path(layout(directory).get("logs", Path(directory).parent.parent))


def output_stem(job):
    return f"{job['item']}_{job['step']}"


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


def valid(root, artifact):
    try:
        path = local_path(root, artifact["path"])
        return (path.is_file() and path.stat().st_size == artifact["size"]
                and sha256_file(path) == artifact["sha256"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
