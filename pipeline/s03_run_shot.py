"""CPU orchestration: a complete shot continues without a local worker."""
import os
from pathlib import Path
import tempfile
import time

from orchestration import model_capabilities, sdk, state
from orchestration.input_store import LIFECYCLE
from orchestration.sdk import cloud_artifact, item_for, snapshot, update
from pipeline.s04_collect_outputs import collect_outputs
from runtime.tasks.task_spec import execution_signature, prepare, result_prefix
from runtime.telemetry.recorder import local_recorder
from runtime.telemetry.schema import utc_now
from orchestration.protocol import VERSION, bindings, run_id, state_path
from runtime.tasks.task_identity import intent_digest
from runtime.utils.files import read_json
from runtime.telemetry.recorder import cloud_recorder

def wait_for_progress(plan, records, backend, poll):
    """For a serial same-model edit chain, wait on its existing Modal call."""
    if len(plan["services"]) == 1 and any(job["uses"].endswith(".i2i") for job in plan["jobs"]):
        active = [r for r in records.values() if r["status"] == "queued" and r.get("call_id")]
        if len(active) == 1 and hasattr(backend, "session"):
            try:
                backend.call_handle(active[0]["call_id"]).get(timeout=poll)
            except Exception:
                # Reconcile through the normal durable polling path.
                pass
            return
    time.sleep(poll)


def run(payload, identity, *, backend=None, root=Path("/data"), timeout=85000, poll=2,
        durable=False, previous=None, single_pass=False):
    """Single claimed owner. Restore only an authoritative checkpoint on takeover."""
    if single_pass and not durable:
        raise ValueError("Single-pass advancement requires a durable owner")
    plan = payload["plan"]
    items = {job["item"] for job in plan["jobs"]}
    if len(items) != 1 or run_id(plan, next(iter(items))) != identity:
        raise ValueError("Cloud workflow identity mismatch")
    backend = backend or sdk.Backend()
    if payload.get("provisioning"):
        from orchestration.protocol import PROVISION_REGISTRY
        registry = backend.modal.Dict.from_name(PROVISION_REGISTRY)
        backend.model_readiness = lambda service: registry.get(payload["provisioning"][service], {})
    backend.collect_outputs = False
    backend.managed_workflow = durable and payload.get("input_lifecycle") == LIFECYCLE
    destination = Path(root) / state_path(identity)
    if single_pass and previous is None and destination.exists():
        raise RuntimeError("Single-pass takeover requires the authoritative checkpoint")
    if not durable and destination.exists():
        raise RuntimeError("Workflow already started; inspect its durable state, never replay")
    if previous is not None:
        from orchestration.workflow_recovery import validate_jobs
        if not durable or previous["payload_hash"] != intent_digest(payload):
            raise ValueError("Recovery checkpoint identity mismatch")
        validate_jobs(plan, previous["jobs"])
        if previous["status"] in {"completed", "failed"}:
            return previous
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary) / plan["project"] / plan["scene"]
        state.save(directory / "plan.json", plan)
        initial = previous["jobs"] if previous is not None else payload["initial_jobs"]
        if set(initial) != {job["key"] for job in plan["jobs"]}:
            raise ValueError("Initial job identities mismatch")
        state.save(directory / "state.json", {
            "plan_hash": plan["hash"], "jobs": initial,
            "endpoints": {key: value["endpoint"] for key, value in bindings(plan).items()}})
        workflow_status = "running"
        backend.recorder = cloud_recorder(Path(root), os.environ.get("MODAL_TASK_ID", "cpu-orchestration"))
        writer = backend.recorder.writer
        scheduler_manifest = writer.reference() if writer is not None else None

        payload_hash = intent_digest(payload)
        last_committed = previous

        def checkpoint(directory, records=None):
            nonlocal last_committed
            if records is None:
                records = sdk.snapshot(directory)[1]["jobs"]
            record = {"version": VERSION, "payload_hash": payload_hash,
                      "status": workflow_status, "jobs": records}
            if workflow_status != "running" and scheduler_manifest:
                record["telemetry"] = scheduler_manifest
            if record != last_committed:
                state.save(destination, record)
                backend.volume.commit()
                last_committed = record

        backend.checkpoint = checkpoint
        backend.defer_checkpoint = True
        try:
            checkpoint(directory)
            while True:
                tick(directory, backend, {}, None)
                records = sdk.snapshot(directory)[1]["jobs"]
                if all(record["status"] in {"completed", "failed"} for record in records.values()):
                    workflow_status = "completed" if all(r["status"] == "completed" for r in records.values()) else "failed"
                    break
                if single_pass:
                    # The owner publishes this running checkpoint in finally.
                    break
                if time.monotonic() - started >= timeout:
                    workflow_status = "uncertain"
                    break
                # Non-admission updates are recoverable from the submission
                # ledger/results. Publish once per round, before sleeping.
                # Terminal rounds are published once in finally with telemetry.
                checkpoint(directory, records)
                wait_for_progress(plan, records, backend, poll)
        except BaseException:
            workflow_status = "uncertain"
            raise
        finally:
            # Scheduler events have their own discoverable manifest, including failures.
            try:
                if writer is not None:
                    try:
                        scheduler_manifest = writer.publish(scheduler_manifest)
                    except Exception:
                        pass  # Keep the original manifest reference on telemetry failure.
                checkpoint(directory)
            finally:
                backend.checkpoint = None
                backend.defer_checkpoint = False
                if writer is not None:
                    writer.close()
        return read_json(destination)


def tick(directory, backend, downloads, pool, *, item=None, pinned_preparation=None, before_submit=None):
    """Advance independent jobs once; never wait for inference or local downloads."""
    directory = Path(directory)
    plan, current = snapshot(directory)
    deferred = getattr(backend, "defer_checkpoint", False)
    readiness_cache = {}

    def advance(key, **changes):
        # A successful commit of the submitting intent is mandatory before
        # EVERY spawn, even when several jobs become ready in the same round.
        # Other updates still reach local state immediately.
        checkpoint = (getattr(backend, "checkpoint", None)
                      if not deferred or changes.get("status") == "submitting" else None)
        update(directory, key, checkpoint=checkpoint, **changes)
    # Pair a shot's first/last images in the cloud queue to unlock its video early.
    order = {job["key"]: index for index, job in enumerate(plan["jobs"])}
    jobs = sorted(plan["jobs"], key=lambda job: (job["item"], order[job["key"]]))
    for job in jobs:
        if item is not None and job["item"] != item:
            continue
        key = job["key"]
        try:
            with state.lock(directory / ".runner", filename=".job-" + intent_digest(key) + ".lock"):
                # Another process or an append may have changed the scene.
                _, current = snapshot(directory)
                records = current["jobs"]
                record = records[key]
                recorder = (backend.recorder.bind(task_id=f"{plan['project']}/{plan['scene']}", job_key=key, call_id=record.get("call_id"))
                            if getattr(backend, "recorder", None) is not None else
                            local_recorder(state.telemetry_directory(directory), task_id=f"{plan['project']}/{plan['scene']}", job_key=key, call_id=record.get("call_id")))
                kind = model_capabilities.CAPABILITIES[job["uses"]]["output"]
                if record["status"] == "submitting":
                    advance(key, status="uncertain", error="Submission interrupted; never automatically resubmit")
                    continue
                if (record.get("call_id") or record["status"] == "uncertain" and record.get("prefix")) and record["status"] not in {"completed", "failed"}:
                    result = None
                    try:
                        result = backend.poll(record)
                        if result is None and record["status"] == "uncertain":
                            accepted = getattr(backend, "accepted_submission_call", None)
                            call_id = accepted(record) if accepted else record.get("call_id")
                            if call_id:
                                advance(key, status="queued", call_id=call_id, error=None)
                        elif result is not None:
                            if result["status"] != "completed":
                                advance(key, status="failed", result=result, error=result.get("error") or "Cloud generation failed")
                            else:
                                artifact = cloud_artifact({**record, "result": result}, kind)
                                advance(key, status="completed", result=result,
                                       artifacts={kind: artifact}, error=None, cloud_observed_at=utc_now())
                    except Exception as exc:
                        # A deterministic remote pre-inference configuration
                        # rejection is terminal. Keep its call ID for audit,
                        # but do not leave it polling as an ambiguous request.
                        if isinstance(exc, RuntimeError) and (
                            "Local configuration differs from deployed model.json" in str(exc)
                            or "Prepared model changed; restart with the current cache" in str(exc)
                            or "Deployment source version mismatch; refusing execution" in str(exc)
                        ):
                            advance(key, status="failed", error=str(exc))
                        else:
                            advance(key, status="uncertain", error=type(exc).__name__ + ": query failed; retained call ID")
                    finally:
                        from runtime.telemetry.collector import enqueue
                        manifest = getattr(backend, "telemetry_manifests", {}).pop(record.get("call_id"), {})
                        if not manifest and isinstance(result, dict):
                            manifest = result.get("telemetry", {})
                        if getattr(backend, "collect_outputs", True):
                            enqueue(state.telemetry_directory(directory), backend.volume,
                                {**manifest, "task_id": f"{plan['project']}/{plan['scene']}"})
                    continue
                if record["status"] not in {"pending", "blocked"}:
                    continue
                service = model_capabilities.CAPABILITIES[job["uses"]]["service"]
                spec = plan["services"][service]["spec"]
                # Dependencies, not downloaded local files, determine readiness.
                dependencies = [value["job"] for value in job["inputs"].values()
                                if isinstance(value, dict) and "job" in value]
                if any(records[dep]["status"] == "failed" for dep in dependencies):
                    advance(key, status="failed", error="Dependency failed")
                    continue
                preparation = pinned_preparation
                readiness = getattr(backend, "model_readiness", None)
                if readiness is not None:
                    # One read per pinned service in this round. Never reuse
                    # readiness across rounds; the GPU invocation still checks
                    # the pinned version and cache contract.
                    if service not in readiness_cache:
                        readiness_cache[service] = readiness(service)
                    preparation = readiness_cache[service]
                    if preparation.get("status") == "failed":
                        advance(key, status="failed", error="Model preparation failed: " + preparation.get("error", "unknown"))
                        continue
                    if preparation.get("status") != "ready":
                        if record.get("wait_reason") != "model":
                            advance(key, status="blocked", wait_reason="model", model_wait_started_at=utc_now())
                        continue
                if record.get("model_wait_started_at"):
                    recorder.interval("wait", record["model_wait_started_at"], reason="model")
                    advance(key, model_wait_started_at=None, wait_reason=None)
                if any(records[dep]["status"] != "completed" for dep in dependencies):
                    if not record.get("dependency_wait_started_at"):
                        advance(key, status="blocked", wait_reason="dependency", dependency_wait_started_at=utc_now())
                    continue
                if record.get("dependency_wait_started_at"):
                    recorder.interval("wait", record["dependency_wait_started_at"], reason="dependency")
                stem = state.output_stem(job)
                suffixes = (".png", ".jpg", ".jpeg", ".webp") if kind == "image" else (".mp4",)
                if any((state.media_directory(directory) / (stem + suffix)).exists() for suffix in suffixes):
                    advance(key, status="failed", error="Untracked local output; refusing GPU submission. Import frames explicitly instead.")
                    continue
                request = item_for(directory, job, records, spec, backend)
                signature = prepare(spec, request)[1]
                fingerprint = (preparation["cache_fingerprint"] if preparation is not None
                               and preparation.get("cache_fingerprint") else backend.fingerprint(spec))
                # Legacy already-pinned batches predate the deployment guard.
                deployment_version = (preparation.get("version") if preparation is not None
                                      and preparation.get("app_revision") else None)
                prefix = result_prefix(spec, plan["project"], plan["scene"], request["operation"])
                receipt = {"id": request["id"], "prefix": prefix, "intent_hash": signature,
                           "cache_fingerprint": fingerprint,
                           "execution_hash": execution_signature(signature, fingerprint)}
                # Persist delivery intent before a generation can be accepted.
                delivery_intent = None
                if plan.get("delivery", {}).get("provider") in {"huggingface", "both"}:
                    delivery_intent = backend.register_delivery(job, receipt, plan)
                    receipt["delivery_id"] = delivery_intent["id"]
                    request = {**request, "_delivery_v3": {"id": delivery_intent["id"],
                                                          "app": plan["delivery_app"]}}
                # Inspect committed evidence before reserving or starting a GPU.
                existing = backend.preflight_result(receipt)
                if existing is not None:
                    if existing["status"] == "completed":
                        artifact = cloud_artifact({**receipt, "result": existing}, kind)
                        advance(key, **receipt, status="completed", result=existing,
                                artifacts={kind: artifact}, error=None, cloud_observed_at=utc_now())
                    elif existing["status"] in {"failed", "cancelled"}:
                        advance(key, **receipt, status="failed", result=existing, error="Cloud generation failed")
                    else:
                        advance(key, **receipt, status="uncertain",
                                error="Existing GPU evidence; observe original submission, never resubmit")
                    continue
                # Durable intent precedes the one and only spawn attempt.
                advance(key, **receipt, status="submitting", wait_reason=None)
                try:
                    if before_submit is not None:
                        before_submit(receipt)
                    with recorder.span("submit") as fields:
                        pin = {"deployment_version": deployment_version} if deployment_version is not None else {}
                        call_id = backend.submit(spec, request, prefix, fingerprint, **pin)
                        fields["call_id"] = call_id
                    advance(key, status="queued", call_id=call_id, submitted_at=utc_now(),
                           dependency_wait_started_at=None)
                    if delivery_intent:
                        # The existing generation reservation also records the call
                        # ID. Failure here must not turn acceptance into resubmission.
                        try:
                            backend.note_delivery_call(delivery_intent, call_id, plan=plan)
                        except Exception as exc:
                            print("Delivery call receipt:", type(exc).__name__, flush=True)
                except BaseException:
                    advance(key, status="uncertain", error="Spawn acceptance unknown; never automatically resubmit")
                    raise
        except RuntimeError as exc:
            if not str(exc).startswith("Another runner holds "):
                raise
            # Only this job is occupied. Other jobs/scenes keep progressing.
            continue
    if not getattr(backend, "collect_outputs", True):
        from orchestration.protocol import report
        return report(plan, snapshot(directory)[1]["jobs"])
    return collect_outputs(directory, backend, downloads, pool, item=item)
