"""Version-one vocabulary shared by all telemetry producers and consumers."""
from datetime import datetime, timezone
import math

VERSION = 1
STAGES = frozenset({"register", "submit", "wait", "startup", "generate", "save", "transfer", "download"})
STATUSES = frozenset({"running", "completed", "failed"})
REASONS = frozenset({"dependency", "lock", "platform"})
CONTEXT_FIELDS = ("task_id", "job_key", "call_id", "container_id", "engine_job_id")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def validate(record):
    if not isinstance(record, dict):
        raise ValueError("Telemetry record must be an object")
    if record.get("schema_version") != VERSION or record.get("record_type") not in {"stage", "resource"}:
        raise ValueError("Unsupported telemetry schema")
    for key in ("event_id", "producer_id", "timestamp"):
        if not isinstance(record.get(key), str) or not record[key]:
            raise ValueError("Missing telemetry field: " + key)
    timestamp = datetime.fromisoformat(record["timestamp"])
    if timestamp.tzinfo is None:
        raise ValueError("Telemetry timestamp needs a timezone")
    for key in CONTEXT_FIELDS:
        if record.get(key) is not None and not isinstance(record[key], str):
            raise ValueError("Invalid telemetry context: " + key)
    if record["record_type"] == "stage":
        if record.get("stage") not in STAGES or record.get("status") not in STATUSES:
            raise ValueError("Unknown telemetry stage/status")
        if record["stage"] == "wait" and record.get("reason") not in REASONS:
            raise ValueError("Unknown telemetry wait reason")
        if not isinstance(record.get("span_id"), str):
            raise ValueError("Missing telemetry span_id")
        duration = record.get("duration_ms")
        if duration is not None and (type(duration) not in (float, int) or not math.isfinite(duration) or duration < 0):
            raise ValueError("Invalid telemetry duration")
        if record.get("measurement") not in {"measured", "derived", "unknown"}:
            raise ValueError("Invalid telemetry measurement")
        for key in ("started_at", "finished_at"):
            if record.get(key) is not None and datetime.fromisoformat(record[key]).tzinfo is None:
                raise ValueError("Telemetry boundary needs a timezone")
        if not isinstance(record.get("started_at"), str):
            raise ValueError("Missing telemetry start boundary")
    else:
        if not isinstance(record.get("values"), dict) or type(record.get("monotonic_ns")) is not int:
            raise ValueError("Invalid telemetry resource sample")
        for key in ("ram_bytes", "ram_peak_bytes", "cpu_seconds", "sample_interval_seconds"):
            value = record["values"].get(key)
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                raise ValueError("Invalid telemetry resource value")
        gpus = record["values"].get("gpus", [])
        if not isinstance(gpus, list) or any(not isinstance(gpu, dict) or type(gpu.get("used_mib")) is not int
                                             or gpu["used_mib"] < 0 for gpu in gpus):
            raise ValueError("Invalid telemetry GPU sample")
    return record
