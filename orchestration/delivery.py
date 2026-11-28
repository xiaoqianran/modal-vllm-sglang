"""Durable scheduling and completion reporting for independent destinations."""
from __future__ import annotations

from pathlib import Path
from time import monotonic

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
            if previous and previous["future"].done() and (
                    previous["epoch"] != epoch or
                    previous.get("next_poll", float("inf")) <= monotonic()):
                del deliveries[key]
            if key in deliveries:
                continue
            durable = plan.get("delivery_protocol") in (2, 3) and destination == "huggingface"
            if destination == "huggingface" and _completed(record, destination):
                if not durable or record["cloud_archive"].get("derivative_status") != "pending":
                    continue
            if destination == "local":
                update(directory, job["key"], download_error=None)
                future = pool.submit(backend.download, directory, job, record, plan)
            else:
                update(directory, job["key"], archive_error=None)
                future = pool.submit(backend.archive, directory, job, record, plan)
            deliveries[key] = {"future": future, "reported": False,
                               "epoch": epoch, "destination": destination, "durable": durable}


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
            elif artifact is None and delivery.get("durable"):
                delivery["next_poll"] = monotonic() + 10
            else:
                update(directory, key[1], cloud_archive=artifact, archive_error=None,
                       archive_collected_at=previous.get("archive_collected_at") or utc_now())
                if delivery.get("durable") and artifact.get("derivative_status") == "pending":
                    delivery["next_poll"] = monotonic() + 15
        except Exception as exc:
            if destination == "local":
                update(directory, key[1], local_artifact=None, download_error=
                       type(exc).__name__ + ": local collection failed; cloud output remains ready")
            else:
                if delivery.get("durable"):
                    _, saved = snapshot(directory)
                    if saved["jobs"][key[1]].get("cloud_archive"):
                        # Optional derivative observation cannot invalidate an
                        # already verified original or set a delivery error.
                        delivery["next_poll"] = monotonic() + 15
                        delivery["reported"] = True
                        continue
                # Preserve known safe contract diagnostics without leaking URLs,
                # credentials, or arbitrary provider exception messages.
                detail = next((message for message in (
                    "Archive deployment version mismatch", "Archive source version mismatch",
                    "Archive batch contract/version mismatch", "Archive receipt mismatch",
                    "Invalid archive result", "Durable archive blocked: blocked_auth",
                    "Durable archive blocked: blocked_integrity", "Durable archive blocked: retry")
                    if message in str(exc)), None)
                if delivery.get("durable") and not detail:
                    delivery["next_poll"] = monotonic() + 15
                update(directory, key[1], archive_error=
                       type(exc).__name__ + ": " + (detail or "cloud archive failed") + "; cloud output remains ready")
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
