"""Small, failure-isolated instrumentation interface; no Modal dependency."""
from contextlib import contextmanager
import threading
import time
import uuid

from runtime.telemetry.schema import CONTEXT_FIELDS, utc_now
from runtime.telemetry.storage import Writer

_local = {}
_lock = threading.Lock()


class Recorder:
    def __init__(self, writer=None, *, sampler=None, **context):
        self.writer = writer
        self.sampler = sampler
        self.context = {key: context.get(key) for key in CONTEXT_FIELDS}

    def bind(self, **context):
        return Recorder(self.writer, sampler=self.sampler, **{**self.context, **context})

    def emit(self, **record):
        try:
            if self.writer is not None:
                self.writer.emit({"schema_version": 1, "event_id": uuid.uuid4().hex,
                                  "producer_id": self.writer.producer_id, "timestamp": utc_now(),
                                  **self.context, **record})
        except Exception:
            pass

    def sample(self):
        try:
            if self.sampler is not None:
                self.sampler()
        except Exception:
            pass

    def resource(self, values):
        self.emit(record_type="resource", monotonic_ns=time.perf_counter_ns(), values=values)

    @contextmanager
    def span(self, stage, *, reason=None, **attributes):
        started_at, started_ns, span_id = utc_now(), time.perf_counter_ns(), uuid.uuid4().hex
        fields = {"record_type": "stage", "span_id": span_id, "stage": stage, "reason": reason,
                  "started_at": started_at, "started_monotonic_ns": started_ns,
                  "measurement": "measured", "attributes": attributes}
        result = {}
        self.emit(**fields, status="running", finished_at=None, duration_ms=None)
        if stage in {"generate", "transfer"}:
            self.sample()
        try:
            yield result
        except BaseException as exc:
            if stage in {"generate", "transfer"}:
                self.sample()
            end = time.perf_counter_ns()
            self.emit(**{**fields, "attributes": {**attributes, **result, "error_type": type(exc).__name__}},
                      status="failed", finished_at=utc_now(), finished_monotonic_ns=end,
                      duration_ms=(end - started_ns) / 1_000_000)
            raise
        else:
            if stage in {"generate", "transfer"}:
                self.sample()
            end = time.perf_counter_ns()
            self.emit(**{**fields, "attributes": {**attributes, **result}}, status="completed",
                      **{key: result[key] for key in CONTEXT_FIELDS if key in result},
                      finished_at=utc_now(), finished_monotonic_ns=end, duration_ms=(end - started_ns) / 1_000_000)

    def interval(self, stage, started_at, *, reason=None, **attributes):
        """Observed cross-process interval; wall-clock-derived, never pure queue time."""
        from datetime import datetime
        finished_at = utc_now()
        try:
            duration = (datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)).total_seconds() * 1000
        except (ValueError, TypeError):
            return
        self.emit(record_type="stage", span_id=uuid.uuid4().hex, stage=stage, reason=reason,
                  status="completed", started_at=started_at, finished_at=finished_at,
                  duration_ms=max(0, duration), measurement="derived", attributes=attributes)


def local_recorder(output, **context):
    from pathlib import Path
    key = str(Path(output).resolve())
    try:
        with _lock:
            writer = _local.get(key)
            if writer is None or writer.closed:
                writer = _local[key] = Writer(Path(output) / ".telemetry/raw/local")
        return Recorder(writer, **context)
    except Exception:
        return Recorder()


def flush_local(output):
    from pathlib import Path
    writer = _local.get(str(Path(output).resolve()))
    return writer.flush() if writer else {"paths": [], "dropped": 0, "errors": 0, "complete": True}


def close_local(output):
    from pathlib import Path
    with _lock:
        writer = _local.pop(str(Path(output).resolve()), None)
    if writer is not None:
        writer.close()


def cloud_recorder(root, container_id):
    try:
        return Recorder(Writer(root / "telemetry", relative="telemetry"), container_id=container_id)
    except Exception:
        return Recorder()
