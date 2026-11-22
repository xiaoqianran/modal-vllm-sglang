"""Durable scheduling and completion reporting for independent destinations."""
from __future__ import annotations

from pathlib import Path

from orchestration import state
from runtime.delivery import destinations
from runtime.telemetry.schema import utc_now
from runtime.utils.files import read_json


def _key(directory, job_key, destination):
    base = (str(Path(directory).resolve()), job_key)
    # Preserve the existing local key for current worker integrations/tests.
    return base if destination == "local" else (*base, destination)


def _completed(record, destination):
    return bool(record.get("local_artifact" if destination == "local" else "cloud_archive"))


def schedule_deliveries(directory, plan, current, backend, deliveries, pool, *, item=None):
    from orchestration.sdk import update
    for job in plan["jobs"]:
        if item is not None and job["item"] != item:
            continue
        record = current["jobs"][job["key"]]
        if record["status"] != "completed":
            continue
        for destination in destinations(plan):
            key = _key(directory, job["key"], destination)
            previous = deliveries.get(key)
            epoch = current.get("collection_epoch", 0)
            if previous and previous["epoch"] != epoch and previous["future"].done():
                del deliveries[key]
            if key in deliveries:
                continue
            if destination == "huggingface" and _completed(record, destination):
                continue
            if destination == "local":
                update(directory, job["key"], download_error=None)
                future = pool.submit(backend.download, directory, job, record, plan)
            else:
                update(directory, job["key"], archive_error=None)
                future = pool.submit(backend.archive, directory, job, record, plan)
            deliveries[key] = {"future": future, "reported": False,
                               "epoch": epoch, "destination": destination}


def collect_finished_deliveries(directory, deliveries):
    from orchestration.sdk import snapshot, update
    scene = str(Path(directory).resolve())
    for key, delivery in list(deliveries.items()):
        if key[0] != scene or delivery["reported"] or not delivery["future"].done():
            continue
        destination = delivery.get("destination", "local")
        try:
            artifact = delivery["future"].result()
            _, saved = snapshot(directory)
            previous = saved["jobs"][key[1]]
            if destination == "local":
                timestamp = {} if previous.get("local_artifact") else {"local_collected_at": utc_now()}
                update(directory, key[1], local_artifact=artifact, download_error=None, **timestamp)
            else:
                update(directory, key[1], cloud_archive=artifact, archive_error=None,
                       archive_collected_at=previous.get("archive_collected_at") or utc_now())
        except Exception as exc:
            if destination == "local":
                update(directory, key[1], local_artifact=None, download_error=
                       type(exc).__name__ + ": local collection failed; cloud output remains ready")
            else:
                update(directory, key[1], archive_error=
                       type(exc).__name__ + ": cloud archive failed; cloud output remains ready")
        delivery["reported"] = True


def publish_report(directory):
    from orchestration.sdk import snapshot
    from orchestration.protocol import report
    plan, current = snapshot(directory)
    result = report(plan, current["jobs"])
    with state.lock(directory, timeout=10):
        saved = read_json(directory / "state.json")
        if saved["plan_hash"] == plan["hash"]:
            if saved.get("registered_at") and not saved.get("finished_at") and all(
                record["status"] == "failed" or
                (record["status"] == "completed" and all(
                    _completed(record, destination) for destination in destinations(plan)))
                for record in saved["jobs"].values()
            ):
                saved["finished_at"] = utc_now()
                state.save(directory / "state.json", saved)
            state.save(directory / "report.json", result)
    return result
