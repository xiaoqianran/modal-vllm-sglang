"""CPU orchestration: a complete shot continues without a local worker."""
import os
from pathlib import Path
import tempfile
import time

from orchestration import sdk, state
from orchestration.protocol import VERSION, bindings, run_id, state_path
from shared.tasks.batch import intent_digest
from shared.utils.files import read_json
from shared.telemetry.recorder import cloud_recorder

def run(payload, identity, *, backend=None, root=Path("/mnt/modal"), timeout=85000, poll=2):
    """Single claimed owner. Every spawn intent and receipt is committed to Volume."""
    plan = payload["plan"]
    items = {job["item"] for job in plan["jobs"]}
    if len(items) != 1 or run_id(plan, next(iter(items))) != identity:
        raise ValueError("Cloud workflow identity mismatch")
    backend = backend or sdk.Backend()
    backend.collect_outputs = False
    destination = Path(root) / state_path(identity)
    if destination.exists():
        raise RuntimeError("Workflow already started; inspect its durable state, never replay")
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary) / plan["project"] / plan["scene"]
        state.save(directory / "plan.json", plan)
        initial = payload["initial_jobs"]
        if set(initial) != {job["key"] for job in plan["jobs"]}:
            raise ValueError("Initial job identities mismatch")
        state.save(directory / "state.json", {
            "plan_hash": plan["hash"], "jobs": initial,
            "endpoints": {key: value["endpoint"] for key, value in bindings(plan).items()}})
        workflow_status = "running"
        backend.recorder = cloud_recorder(Path(root), os.environ.get("MODAL_TASK_ID", "cpu-orchestration"))
        writer = backend.recorder.writer
        scheduler_manifest = writer.reference() if writer is not None else None

        def checkpoint(directory):
            current = sdk.snapshot(directory)[1]
            record = {"version": VERSION, "payload_hash": intent_digest(payload),
                      "status": workflow_status, "jobs": current["jobs"]}
            if workflow_status != "running" and scheduler_manifest:
                record["telemetry"] = scheduler_manifest
            state.save(destination, record)
            backend.volume.commit()

        backend.checkpoint = checkpoint
        try:
            checkpoint(directory)
            while True:
                sdk.tick(directory, backend, {}, None)
                records = sdk.snapshot(directory)[1]["jobs"]
                if all(record["status"] in {"completed", "failed"} for record in records.values()):
                    workflow_status = "completed" if all(r["status"] == "completed" for r in records.values()) else "failed"
                    break
                if time.monotonic() - started >= timeout:
                    workflow_status = "uncertain"
                    break
                time.sleep(poll)
        except BaseException:
            workflow_status = "uncertain"
            raise
        finally:
            # Scheduler events have their own discoverable manifest, including failures.
            try:
                checkpoint(directory)
                writer = backend.recorder.writer
                if writer is not None:
                    try:
                        manifest = writer.publish(scheduler_manifest)
                        current = sdk.snapshot(directory)[1]
                        state.save(destination, {"version": VERSION, "payload_hash": intent_digest(payload),
                                   "status": workflow_status, "jobs": current["jobs"], "telemetry": manifest})
                        backend.volume.commit()
                    except Exception:
                        pass  # Optional evidence must not change completed generation.
            finally:
                backend.checkpoint = None
                if writer is not None:
                    writer.close()
        return read_json(destination)
