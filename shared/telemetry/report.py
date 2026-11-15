"""Reports from indexed evidence, never from guessed platform timings."""
from datetime import datetime
import json
from pathlib import Path

from shared.telemetry.storage import collect_local, connect
from shared.telemetry.schema import STAGES
from shared.utils.files import atomic_json, read_json, local_path


def manifest_for(output, job):
    manifest = job.get("result", {}).get("telemetry")
    if manifest and manifest.get("manifest_path"):
        from shared.tasks.batch import intent_digest
        cached = Path(output) / ".telemetry/raw/manifests" / (intent_digest(manifest["manifest_path"]) + ".json")
        if cached.is_file():
            return read_json(cached)
    return manifest


def task_report(output, task_id, *, refresh=True):
    directory = local_path(output, task_id)
    current = read_json(directory / "state.json") if (directory / "state.json").exists() else {"jobs": {}}
    if refresh:
        collect_local(output)
    with connect(output) as db:
        records = [json.loads(row[0]) for row in db.execute("SELECT data FROM events WHERE task_id=?", (task_id,))]
        producers = {record["producer_id"] for record in records if record.get("container_id")}
        for producer in producers:
            records.extend(json.loads(row[0]) for row in db.execute(
                "SELECT data FROM events WHERE producer_id=? AND task_id IS NULL", (producer,)))
    records.sort(key=lambda record: (record["timestamp"], record["event_id"]))
    spans = {}
    samples = [record for record in records if record["record_type"] == "resource"]
    for record in records:
        if record["record_type"] != "stage":
            continue
        previous = spans.get(record["span_id"])
        if previous is None or previous["status"] == "running" or record["status"] != "running":
            spans[record["span_id"]] = dict(record)
    for span in spans.values():
        job = current.get("jobs", {}).get(span.get("job_key"), {})
        if not span.get("call_id") and job.get("call_id"):
            span["call_id"] = job["call_id"]
            span["call_id_linked_from_state"] = True
    totals = {stage: 0.0 for stage in sorted(STAGES)}
    for span in spans.values():
        # Startup shared by several tasks is listed, not charged as each task's work.
        if span.get("task_id") == task_id and span.get("duration_ms") is not None:
            totals[span["stage"]] += span["duration_ms"]
        start, end = span.get("started_monotonic_ns"), span.get("finished_monotonic_ns")
        inside = [sample["values"] for sample in sorted(samples, key=lambda sample: sample["monotonic_ns"])
                  if start is not None and end is not None and sample["producer_id"] == span["producer_id"]
                  and start <= sample["monotonic_ns"] <= end]
        ram = [sample["ram_bytes"] for sample in inside if "ram_bytes" in sample]
        gpu = [value["used_mib"] for sample in inside for value in sample.get("gpus", [])]
        cpu = [sample["cpu_seconds"] for sample in inside if "cpu_seconds" in sample]
        span["resources"] = {"sample_count": len(inside), "sampled_ram_peak_bytes": max(ram) if ram else None,
                             "sampled_gpu_peak_mib": max(gpu) if gpu else None,
                             "sampled_cpu_seconds": max(0, cpu[-1] - cpu[0]) if len(cpu) >= 2 else None}
    task_stages = [record for record in records if record["record_type"] == "stage" and record.get("task_id") == task_id]
    elapsed = None
    if task_stages:
        earliest = min(datetime.fromisoformat(record["started_at"]) for record in task_stages)
        latest = max(datetime.fromisoformat(record.get("finished_at") or record["timestamp"]) for record in task_stages)
        elapsed = max(0, (latest - earliest).total_seconds() * 1000)
    result = {"schema_version": 1, "task_id": task_id, "event_count": len(records),
              "observed_elapsed_ms": elapsed, "stage_totals_ms": totals,
              "unclosed_spans": sum(span["status"] == "running" for span in spans.values()),
              "platform_wait_ms": None, "snapshot_restore_ms": None,
              "missing_stages": sorted(STAGES - {span["stage"] for span in spans.values()}),
              "stages": list(spans.values()), "resource_samples": samples,
              "jobs": {key: {"status": job["status"], "call_id": job.get("call_id"),
                             "telemetry": manifest_for(output, job),
                             "telemetry_error": job.get("result", {}).get("telemetry_error"),
                             "local_downloaded": bool(job.get("local_artifact")),
                             "missing_stages": sorted({"submit", "generate", "save", "download"} -
                                 {span["stage"] for span in spans.values() if span.get("job_key") == key})}
                       for key, job in current.get("jobs", {}).items()},
              "notes": ["Stage totals can overlap; do not sum them as end-to-end time.",
                        "Platform queue and snapshot restore durations are unknown without platform evidence.",
                        "Resource peaks are sampled container measurements, not exclusive job allocation."]}
    result["local_producers"] = []
    for path in (Path(output) / ".telemetry/raw/local").glob("*/status.json"):
        try:
            result["local_producers"].append(read_json(path))
        except (OSError, ValueError):
            result["local_producers"].append({"errors": 1, "complete": False})
    collector_status = Path(output) / ".telemetry/collector.json"
    if collector_status.is_file():
        result["collector"] = read_json(collector_status)
    # Use a digest for filenames; task_id remains the public, human-readable identity.
    from shared.tasks.batch import intent_digest
    atomic_json(Path(output) / ".telemetry/reports" / (intent_digest(task_id) + ".json"), result)
    return result
