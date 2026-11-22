"""Collect completed artifacts and publish the local report."""
from orchestration import state
from runtime.utils.files import read_json
from runtime.telemetry.schema import utc_now

def collect_outputs(directory, backend, downloads, pool, *, item=None):
    from orchestration.sdk import snapshot, update
    plan, current = snapshot(directory)
    for job in plan["jobs"]:
        if item is not None and job["item"] != item:
            continue
        key = job["key"]
        record = current["jobs"][key]
        download_key = (str(directory.resolve()), key)
        epoch = current.get("collection_epoch", 0)
        previous = downloads.get(download_key)
        if previous and previous["epoch"] != epoch and previous["future"].done():
            del downloads[download_key]
        if record["status"] == "completed" and download_key not in downloads:
            update(directory, key, download_error=None)
            downloads[download_key] = {"future": pool.submit(backend.download, directory, job, record, plan),
                                       "reported": False, "epoch": epoch}
    for (scene, key), download in list(downloads.items()):
        future = download["future"]
        if scene != str(directory.resolve()) or not future.done() or download["reported"]:
            continue
        # Keep finished futures to avoid retry loops after a disk/download failure.
        try:
            artifact = future.result()
            _, saved = snapshot(directory)
            timestamps = {} if saved["jobs"][key].get("local_artifact") else {"local_collected_at": utc_now()}
            update(directory, key, local_artifact=artifact, download_error=None, **timestamps)
        except Exception as exc:
            update(directory, key, local_artifact=None,
                   download_error=type(exc).__name__ + ": local collection failed; cloud output remains ready")
        download["reported"] = True
    plan, current = snapshot(directory)
    from orchestration.protocol import report
    result = report(plan, current["jobs"])
    with state.lock(directory, timeout=10):
        # Reports are derived snapshots. Do not overwrite a newer appended scene's report.
        saved = read_json(directory / "state.json")
        if saved["plan_hash"] == plan["hash"]:
            if saved.get("registered_at") and not saved.get("finished_at") and all(
                r["status"] == "failed" or r["status"] == "completed" and r.get("local_artifact")
                for r in saved["jobs"].values()):
                saved["finished_at"] = utc_now()
                state.save(directory / "state.json", saved)
            state.save(directory / "report.json", result)
    return result
