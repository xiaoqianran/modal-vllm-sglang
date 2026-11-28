"""A file-based dashboard: no server, cloud credentials or private paths in media."""
import json
import shutil
import struct
import subprocess
from functools import lru_cache
from pathlib import Path

from orchestration import state
from runtime.utils.files import read_json, atomic_write


@lru_cache(maxsize=256)
def _dimensions(path, modified, size):
    try:
        with Path(path).open("rb") as stream:
            header = stream.read(24)
        if header.startswith(b"\x89PNG\r\n\x1a\n") and header[12:16] == b"IHDR":
            return struct.unpack(">II", header[16:24])
        probe = shutil.which("ffprobe")
        if probe:
            result = subprocess.run([probe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                                     "stream=width,height", "-of", "json", path],
                                    capture_output=True, text=True, timeout=5, check=True)
            info = json.loads(result.stdout)["streams"][0]
            return info["width"], info["height"]
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
        pass
    return None


def dimensions(path):
    try:
        stat = path.stat()
        return _dimensions(str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


def duration(value):
    if value is None:
        return "未采集"
    if value < 1000:
        return f"{value:.0f} 毫秒"
    if value < 60000:
        return f"{value / 1000:.1f} 秒"
    return f"{int(value // 60000)} 分 {int(value // 1000) % 60} 秒"


def safe_json(data):
    return json.dumps(data, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


# One reconstructible, process-local cache. The task files remain authoritative.
_monitor_cache = {}


def render(output):
    from orchestration.monitor_data import build_snapshot
    from runtime.tasks.task_identity import intent_digest
    from orchestration import studio

    output = Path(output).resolve()
    runtime = state.runtime_root(output)
    cache = _monitor_cache.setdefault(output, {})
    shared = (output / studio.RECEIPTS, output / ".runtime/preview-index.json")
    shared_stamp = tuple(state.file_stamp(path) for path in shared)
    archive_receipts = studio.read_json(shared[0], {}) or {}
    snapshots, seen = [], set()
    changed = False
    for path in sorted((runtime / "tasks").glob("*/*/plan.json")):
        directory = path.parent
        base = (path, directory / "state.json", directory / "layout.json")
        initial = tuple(state.file_stamp(file) for file in base)
        if initial[1] is None:
            continue
        cached = cache.get(path)
        if cached is not None:
            watched, signature, data, include = cached
            if signature == (initial, shared_stamp, tuple(state.file_stamp(file) for file in watched)):
                seen.add(path)
                snapshots.append(data if include else {**data, "jobs": []})
                feed = runtime / "monitor" / (data["name"] + ".js")
                if not feed.is_file():
                    atomic_write(feed, ("window.updateTaskMonitor(" + safe_json(data) + ");").encode("utf-8"))
                continue
        layout = state.layout(directory)
        if layout.get("output") != str(output):
            continue
        plan, current = read_json(path), read_json(base[1])
        if current.get("plan_hash") != plan.get("hash"):
            # A concurrent registration/append has not published both files yet.
            continue
        task_id = f"{plan['project']}/{plan['scene']}"
        watched = (runtime / "logs/.telemetry/reports" / (intent_digest(task_id) + ".json"),
                   *(runtime / "models" / (name + ".json") for name in plan["services"]),
                   *(Path(layout.get("media", directory)) / record["local_artifact"]["path"]
                     for record in current["jobs"].values() if record.get("local_artifact")))
        signature = (initial, shared_stamp, tuple(state.file_stamp(file) for file in watched))
        data = build_snapshot(directory, plan, current, dimensions, archive_receipts=archive_receipts)
        data["monitor_static"] = not state.pending_items(plan, current)
        include = not plan.get("work")
        seen.add(path)
        snapshots.append(data if include else {**data, "jobs": []})
        feed = runtime / "monitor" / (plan["name"] + ".js")
        atomic_write(feed, ("window.updateTaskMonitor(" + safe_json(data) + ");").encode("utf-8"))
        if data["monitor_static"] and initial == tuple(state.file_stamp(file) for file in base):
            cache[path] = (watched, signature, data, include)
        else:
            cache.pop(path, None)
        changed = True
    removed = set(cache) - seen
    for path in removed:
        cache.pop(path, None)
    # Publish only when a feed changes; completed task feeds need no heartbeat.
    catalog = output / ".runtime/studio-catalog.js"
    if studio.TEMPLATE.is_file() and (snapshots or removed):
        if changed or removed or not catalog.is_file():
            studio.publish_catalog(output, snapshots)
        if not (output / "index.html").is_file():
            studio.render(output, thumbs=False)


class LogStream:
    """Preserve terminal output while bounding the local worker log."""
    def __init__(self, original, path):
        import logging
        from logging.handlers import RotatingFileHandler
        self.original = original
        self.handler = RotatingFileHandler(path, maxBytes=8 * 1024 * 1024, backupCount=3, encoding="utf-8")
        self.handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))

    def write(self, value):
        import logging
        self.original.write(value)
        if value.strip():
            self.handler.handle(logging.LogRecord("worker", logging.INFO, "", 0, value.rstrip(), (), None))
        return len(value)

    def flush(self):
        self.original.flush()
        self.handler.flush()

    def close(self):
        self.handler.close()
