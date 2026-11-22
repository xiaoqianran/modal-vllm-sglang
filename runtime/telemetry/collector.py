"""Incremental local/cloud shard collection. No model execution or state mutation."""
from contextlib import contextmanager
import json
from pathlib import Path
import queue
import re
import sqlite3
import threading
import time

from runtime.telemetry.schema import validate
from runtime.config.storage import volume_key
from runtime.utils.files import atomic_json, download_volume_file, sha256_file, read_json

MAX_SHARD = 4 * 1024 * 1024
_collectors = {}
_lock = threading.Lock()


from runtime.telemetry.storage import connect, ingest, collect_local


def collect_cloud(output, volume, manifest):
    summary = {"events": 0, "errors": 0, "pending": False}
    pointer = manifest.get("manifest_path")
    if pointer:
        try:
            if not re.fullmatch(r"telemetry/[0-9a-f]{32}/manifest-[0-9a-f]{32}\.json", pointer):
                raise ValueError("Invalid telemetry manifest reference")
            chunks, size = [], 0
            for chunk in volume.read_file(volume_key("/" + pointer)):
                size += len(chunk)
                if size > MAX_SHARD:
                    raise ValueError("Telemetry manifest too large")
                chunks.append(chunk)
            manifest = json.loads(b"".join(chunks))
            if not isinstance(manifest, dict) or not isinstance(manifest.get("paths"), list):
                raise ValueError("Invalid telemetry manifest")
            summary["pending"] = not (manifest.get("complete", False) or manifest.get("final", False))
            from runtime.tasks.task_identity import intent_digest
            atomic_json(Path(output) / ".telemetry/raw/manifests" / (intent_digest(pointer) + ".json"), manifest)
        except FileNotFoundError:
            return {"events": 0, "errors": 0, "pending": True}
        except (OSError, ValueError, TypeError):
            return {"events": 0, "errors": 1, "pending": True}
    for ref in manifest.get("paths", []):
        try:
            path = ref["path"]
            if (not isinstance(path, str) or not re.fullmatch(r"telemetry/[0-9a-f]{32}/[0-9]{8}\.jsonl", path)
                    or not re.fullmatch(r"[0-9a-f]{64}", ref["sha256"])
                    or type(ref["bytes"]) is not int or not 0 < ref["bytes"] <= MAX_SHARD):
                raise ValueError("Invalid telemetry shard reference")
            target = Path(output) / ".telemetry/raw/cloud" / path.removeprefix("telemetry/")
            # A cached copy is reusable only while it matches the cloud manifest.
            # Re-download atomically so a failed repair cannot publish partial data.
            if (not target.exists() or target.stat().st_size != ref["bytes"]
                    or sha256_file(target) != ref["sha256"]):
                download_volume_file(volume, volume_key("/" + path), target, expected_sha256=ref["sha256"],
                                     max_bytes=ref["bytes"])
            if target.stat().st_size != ref["bytes"] or sha256_file(target) != ref["sha256"]:
                raise ValueError("Telemetry shard checksum mismatch")
            summary["events"] += ingest(output, target)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            summary["errors"] += 1
    return summary


class Collector:
    def __init__(self, output, volume):
        self.output, self.volume = Path(output), volume
        self.queue = queue.Queue(maxsize=256)
        self.errors = 0
        self.dropped = 0
        self.tasks = set()
        self.pending = {}
        self.completed = set()
        self.attempts = {}
        self.next_attempt = {}
        self.seen = {}
        self.state_stamps = {}
        self.event_counts = {}
        self.exhausted = {}
        self.last_scan = 0
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="telemetry-collector")
        self.thread.start()

    def submit(self, manifest):
        try:
            self.queue.put_nowait(manifest)
        except queue.Full:
            self.dropped += 1

    def _run(self):
        while not self.done.is_set():
            try:
                manifest = self.queue.get(timeout=2)
            except queue.Empty:
                manifest = None
            try:
                if manifest:
                    key = manifest.get("manifest_path") or str(manifest.get("paths"))
                    if key not in self.completed and key not in self.pending:
                        self.pending[key] = manifest
                    if manifest.get("task_id"):
                        self.tasks.add(manifest["task_id"])
                # Recover both successful and failed jobs after a worker restart.
                # Discovery is independent of generation polling and queue delivery.
                if time.monotonic() - self.last_scan >= 10:
                    for path in state_paths(self.output):
                        stamp = (path.stat().st_mtime_ns, path.stat().st_size)
                        if self.state_stamps.get(str(path)) == stamp:
                            continue
                        current = read_json(path)
                        task_id = f"{path.parent.parent.name}/{path.parent.name}"
                        self.tasks.add(task_id)
                        self.event_counts.pop(task_id, None)
                        refs = [r.get("result", {}).get("telemetry") for r in current.get("jobs", {}).values()]
                        refs += [r.get("telemetry") for r in current.get("cloud_runs", {}).values()]
                        for ref in refs:
                            if ref:
                                key = ref.get("manifest_path") or str(ref.get("paths"))
                                if key not in self.completed:
                                    self.pending.setdefault(key, {**ref, "task_id": task_id})
                        self.state_stamps[str(path)] = stamp
                    self.last_scan = time.monotonic()
                for key, pending in list(self.pending.items()):
                    if time.monotonic() < self.next_attempt.get(key, 0):
                        continue
                    result = collect_cloud(self.output, self.volume, pending)
                    self.errors += result["errors"]
                    if pending.get("task_id"):
                        self.event_counts.pop(pending["task_id"], None)
                    if not result["pending"] and not result["errors"]:
                        del self.pending[key]
                        self.completed.add(key)
                    else:
                        self.attempts[key] = self.attempts.get(key, 0) + 1
                        if self.attempts[key] >= 12:
                            self.exhausted[key] = "Publication unavailable; explicit collect can retry"
                            self.completed.add(key)
                            del self.pending[key]
                        else:
                            self.next_attempt[key] = time.monotonic() + min(300, 2 ** self.attempts[key])
                self.errors += collect_local(self.output, seen=self.seen)["errors"]
                from runtime.telemetry.report import task_report
                with connect(self.output) as db:
                    counts = dict(db.execute("SELECT task_id, count(*) FROM events WHERE task_id IS NOT NULL GROUP BY task_id"))
                for task_id in self.tasks:
                    if self.event_counts.get(task_id) != counts.get(task_id, 0):
                        task_report(self.output, task_id, refresh=False)
                        self.event_counts[task_id] = counts.get(task_id, 0)
                # Diagnostics are separate from generation state.
                if (self.output / ".telemetry").exists():
                    atomic_json(self.output / ".telemetry/collector.json", {"errors": self.errors,
                                "dropped": self.dropped, "pending": len(self.pending), "exhausted": self.exhausted})
            except Exception:
                self.errors += 1
            finally:
                if manifest is not None:
                    self.queue.task_done()

    def close(self):
        self.done.set()
        self.thread.join(timeout=3)


def enqueue(output, volume, manifest):
    """Called by scheduling code; remote collection happens in another thread."""
    try:
        if not manifest.get("paths") and not manifest.get("manifest_path"):
            return
        key = str(Path(output).resolve())
        with _lock:
            if key not in _collectors:
                _collectors[key] = Collector(output, volume)
            _collectors[key].submit(manifest)
    except Exception:
        pass


def start(output, volume):
    """Start recovery even if no new job ever reaches a terminal state."""
    key = str(Path(output).resolve())
    with _lock:
        if key not in _collectors:
            _collectors[key] = Collector(output, volume)
    return _collectors[key]


def state_paths(output):
    output = Path(output)
    roots = {output, output.parent / ".runtime/tasks"}
    if output.name == "logs" and output.parent.name == ".runtime":
        roots.add(output.parent / "tasks")
    return sorted({p for root in roots for p in root.glob("*/*/state.json")})


def collect(output, volume=None):
    """Explicit recovery collection; cloud access is optional, never starts a GPU."""
    summary = collect_local(output)
    summary["pending"] = 0
    if volume is not None:
        for path in state_paths(output):
            current = read_json(path)
            for record in current.get("jobs", {}).values():
                manifest = record.get("result", {}).get("telemetry", {})
                result = collect_cloud(output, volume, manifest)
                for key in summary:
                    summary[key] += result.get(key, 0)
            for record in current.get("cloud_runs", {}).values():
                if record.get("telemetry"):
                    result = collect_cloud(output, volume, record["telemetry"])
                    for key in summary:
                        summary[key] += result.get(key, 0)
    return summary
