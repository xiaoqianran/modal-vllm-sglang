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


def render(output):
    from orchestration.monitor_data import build_snapshot
    output = Path(output).resolve()
    runtime = state.runtime_root(output)
    from orchestration import studio
    archive_receipts = studio.read_json(output / studio.RECEIPTS, {}) or {}
    snapshots = []
    for path in sorted((runtime / "tasks").glob("*/*/plan.json")):
        directory = path.parent
        if not (directory / "state.json").exists() or state.layout(directory).get("output") != str(output):
            continue
        plan, current = read_json(path), read_json(directory / "state.json")
        data = build_snapshot(directory, plan, current, dimensions, archive_receipts=archive_receipts)
        snapshots.append(data)
        encoded = safe_json(data)
        atomic_write(runtime / "monitor" / (plan["name"] + ".js"), ("window.updateTaskMonitor(" + encoded + ");").encode("utf-8"))
    # The live worker already built these snapshots. Publish a small shared
    # catalogue; never re-encode thumbnails or contact HF in the polling loop.
    if studio.TEMPLATE.is_file() and snapshots:
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
