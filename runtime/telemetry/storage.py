"""Bounded background writes into immutable, independently owned JSONL shards."""
import atexit
import hashlib
import json
from pathlib import Path
import queue
import threading
import time
import uuid

from runtime.telemetry.schema import validate
from runtime.utils.files import atomic_write

_writers = []


class Writer:
    def __init__(self, root, *, relative="", capacity=4096):
        self.producer_id = uuid.uuid4().hex
        self.directory = Path(root) / self.producer_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self.relative = relative.rstrip("/")
        self.queue = queue.Queue(maxsize=capacity)
        self.lock = threading.Lock()
        self.paths = []
        self.startup_paths = []
        self.published_count = 0
        self.dropped = 0
        self.errors = 0
        self.sequence = 0
        self.closed = False
        self.thread = threading.Thread(target=self._run, daemon=True, name="telemetry-writer")
        self.thread.start()
        _writers.append(self)

    def emit(self, record):
        try:
            validate(record)
            if self.closed:
                raise RuntimeError("Telemetry writer closed")
            self.queue.put_nowait(record)
        except Exception:
            self.dropped += 1

    def _run(self):
        while True:
            first = self.queue.get()
            if first is None:
                self.queue.task_done()
                return
            if isinstance(first, threading.Event):
                first.set()
                self.queue.task_done()
                continue
            if isinstance(first, tuple):
                self._publish_control(first)
                self.queue.task_done()
                continue
            batch, control, has_control = [first], None, False
            # Briefly coalesce cloud events; FIFO control/barrier messages
            # bypass the batching window, so no synchronous publication waits
            # for the timer. Local logs keep their immediate-write behavior.
            deadline = time.monotonic() + (0.25 if self.relative else 0)
            while len(batch) < 128:
                try:
                    value = self.queue.get(timeout=max(0, deadline - time.monotonic()))
                except queue.Empty:
                    break
                if not isinstance(value, dict):
                    control, has_control = value, True
                    break
                batch.append(value)
            try:
                data = ("\n".join(json.dumps(record, ensure_ascii=False, allow_nan=False) for record in batch) + "\n").encode()
                if len(data) > 4 * 1024 * 1024:
                    raise ValueError("Telemetry shard too large")
                filename = f"{self.sequence:08}.jsonl"
                self.sequence += 1
                if not self.directory.is_dir():
                    raise FileNotFoundError("Telemetry directory removed")
                atomic_write(self.directory / filename, data)
                path = "/".join(filter(None, (self.relative, self.producer_id, filename)))
                with self.lock:
                    ref = {"path": path, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                    self.paths.append(ref)
                    if any(record.get("stage") == "startup" for record in batch):
                        self.startup_paths.append(ref)
            except Exception:
                self.errors += 1
                self.dropped += len(batch)
            finally:
                for _ in batch:
                    self.queue.task_done()
                if has_control:
                    if isinstance(control, threading.Event):
                        control.set()
                    elif isinstance(control, tuple):
                        self._publish_control(control)
                    self.queue.task_done()
            if has_control and control is None:
                return

    def flush(self, timeout=2):
        if self.closed:
            return self.manifest()
        barrier = threading.Event()
        try:
            self.queue.put(barrier, timeout=timeout)
            complete = barrier.wait(timeout)
        except queue.Full:
            complete = False
        try:
            from runtime.utils.files import atomic_json
            if self.directory.is_dir():
                atomic_json(self.directory / "status.json", {"producer_id": self.producer_id,
                            "dropped": self.dropped, "errors": self.errors, "complete": complete})
        except Exception:
            self.errors += 1
        return {**self.manifest(), "complete": complete}

    def manifest(self):
        with self.lock:
            return {"schema_version": 1, "paths": list(self.paths), "dropped": self.dropped, "errors": self.errors}

    def reference(self):
        filename = "manifest-" + uuid.uuid4().hex + ".json"
        return {"schema_version": 1, "manifest_path": "/".join(filter(None, (self.relative, self.producer_id, filename))),
                "paths": [], "complete": False}

    def publish(self, reference):
        from runtime.utils.files import atomic_json
        flushed = self.flush()
        manifest = self._publication(reference, flushed.get("complete", False))
        atomic_json(self.directory / Path(reference["manifest_path"]).name, manifest)
        return manifest

    def _publication(self, reference, complete):
        with self.lock:
            paths = {ref["path"]: ref for ref in self.startup_paths + self.paths[self.published_count:]}
            if complete:
                self.published_count = len(self.paths)
            return {"schema_version": 1, "manifest_path": reference["manifest_path"],
                    "paths": list(paths.values()), "complete": complete, "final": True,
                    "dropped": self.dropped, "errors": self.errors}

    def publish_async(self, reference, commit):
        """Publication is a FIFO barrier handled by the writer, outside the GPU call."""
        try:
            self.queue.put_nowait((reference, commit))
        except queue.Full:
            self.dropped += 1

    def _publish_control(self, control):
        from runtime.utils.files import atomic_json
        reference, commit = control
        try:
            manifest = self._publication(reference, True)
            atomic_json(self.directory / Path(reference["manifest_path"]).name, manifest)
            commit()
        except Exception:
            self.errors += 1

    def close(self):
        if self.closed:
            return
        self.flush()
        self.closed = True
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass
        self.thread.join(timeout=2)


def close_all():
    for writer in list(_writers):
        writer.close()


atexit.register(close_all)


from contextlib import contextmanager
import sqlite3
from runtime.utils.files import sha256_file
MAX_SHARD = 4 * 1024 * 1024

@contextmanager
def connect(output):
    directory = Path(output) / ".telemetry"
    directory.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(directory / "index.sqlite", timeout=5)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, task_id TEXT, producer_id TEXT, record_type TEXT, data TEXT NOT NULL)")
    db.execute("CREATE INDEX IF NOT EXISTS events_task ON events(task_id)")
    db.execute("CREATE INDEX IF NOT EXISTS events_producer ON events(producer_id)")
    db.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, sha256 TEXT NOT NULL)")
    try:
        with db:
            yield db
    finally:
        db.close()


def ingest(output, path):
    path = Path(path)
    if path.stat().st_size > MAX_SHARD:
        raise ValueError("Telemetry shard too large")
    digest = sha256_file(path)
    with connect(output) as db:
        old = db.execute("SELECT sha256 FROM files WHERE path=?", (str(path.resolve()),)).fetchone()
        if old:
            if old[0] != digest:
                raise ValueError("Immutable telemetry shard changed")
            return 0
        records = [validate(json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines() if line]
        count = 0
        for record in records:
            data = json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False)
            previous = db.execute("SELECT data FROM events WHERE event_id=?", (record["event_id"],)).fetchone()
            if previous and previous[0] != data:
                raise ValueError("Telemetry event identity conflict")
            if not previous:
                db.execute("INSERT INTO events VALUES (?,?,?,?,?)", (record["event_id"], record.get("task_id"),
                           record["producer_id"], record["record_type"], data))
                count += 1
        db.execute("INSERT INTO files VALUES (?,?)", (str(path.resolve()), digest))
    return count


def collect_local(output, *, seen=None):
    summary = {"events": 0, "errors": 0}
    for path in (Path(output) / ".telemetry/raw").glob("**/*.jsonl"):
        try:
            stamp = (path.stat().st_mtime_ns, path.stat().st_size)
            if seen is not None and seen.get(str(path)) == stamp:
                continue
            summary["events"] += ingest(output, path)
            if seen is not None:
                seen[str(path)] = stamp
        except (OSError, ValueError, sqlite3.Error):
            summary["errors"] += 1
    return summary
