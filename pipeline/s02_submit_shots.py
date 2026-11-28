"""Bounded local delivery and collection; dependency scheduling lives in Modal."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import copy
import json
import math
from pathlib import Path
import time

from orchestration import sdk, state
from orchestration.input_store import LIFECYCLE, MANAGED_PREFIX
from pipeline.s04_collect_outputs import collect_outputs
from orchestration.protocol import APP_NAME, VERSION, run_id, state_path
from runtime.tasks.task_identity import intent_digest
from runtime.telemetry.collector import enqueue
from runtime.telemetry.recorder import local_recorder
from runtime.config.storage import volume_key
from runtime.utils.files import read_json
from runtime.tasks.artifacts import image_reference, IMAGE_FIELDS, image_limit


class Backend(sdk.Backend):
    def persist_workflow(self, payload, identity):
        from orchestration.workflow_recovery import publish_plan
        from runtime.config.storage import runtime_volume_name
        if payload.get("recovery_schema") == 1:
            return publish_plan(self.volume, payload, identity, runtime_volume_name())

    def submit_workflow(self, payload, identity, *, deployment_version=None, persisted=False):
        from runtime.serving.deployment_probe import versioned_name
        if not persisted:
            self.persist_workflow(payload, identity)
        tag = versioned_name("run_shot", deployment_version)
        guard = {"expected_deployment_version": deployment_version} if deployment_version is not None else {}
        lookup = lambda: self.modal.Function.from_name(APP_NAME, tag)
        function = (self.session.handle(("workflow", APP_NAME, tag, deployment_version), lookup)
                    if deployment_version is not None else lookup())
        return function.spawn(payload, identity, **guard).object_id

    def poll_workflow(self, receipt):
        try:
            data = bytearray()
            for chunk in self.volume.read_file(volume_key("/" + state_path(receipt["id"]))):
                data.extend(chunk)
                if len(data) > 16 * 1024 * 1024:
                    raise ValueError("Workflow state too large")
            result = json.loads(data)
        except FileNotFoundError:
            if not receipt.get("call_id"):
                return None
            from modal.exception import TimeoutError as ModalTimeoutError
            try:
                result = self.call_handle(receipt["call_id"]).get(timeout=0)
            except (TimeoutError, ModalTimeoutError) as exc:
                if type(exc) in (TimeoutError, ModalTimeoutError):
                    return None
                self.session.forget_handle(("call", receipt["call_id"]))
                raise
            except Exception:
                self.session.forget_handle(("call", receipt["call_id"]))
                raise
        if result.get("version") != VERSION or result.get("payload_hash") != receipt["payload_hash"]:
            raise ValueError("Workflow receipt/configuration mismatch")
        if result.get("status") in {"running", "uncertain"}:
            result = self._observe_controller(receipt, result)
        if receipt.get("call_id") and result.get("status") in {"completed", "failed", "uncertain"}:
            self.session.forget_handle(("call", receipt["call_id"]))
        return result


    def _observe_controller(self, receipt, result):
        from orchestration import workflow_recovery as recovery
        from runtime.config.storage import runtime_volume_name

        key = ("controller", receipt["id"], receipt["payload_hash"])
        controller_state = receipt.get("controller_state", "unconfirmed")
        if self.session.ledger_due(key, 10):
            self.session.note_ledger_read(key)
            controller_state = "unconfirmed"
            try:
                manifest = recovery.load_plan(self.volume, receipt["id"], runtime_volume_name(),
                                              expected_hash=receipt["payload_hash"])
            except FileNotFoundError:
                manifest = None  # Legacy checkpoints have no recoverable owner chain.
            if manifest is not None:
                owner = recovery.latest_owner(self.volume, manifest)
                if owner is not None:
                    controller_state = recovery.observe_owner(self.volume, manifest, self.modal, owner)
        if controller_state == "terminal":
            return {**result, "status": "uncertain", "controller_state": controller_state,
                    "error": "Controller terminated; durable recovery available (resume_shot)"}
        if controller_state != "active":
            return {**result, "status": "uncertain", "controller_state": controller_state,
                    "error": "Controller state unconfirmed; refusing takeover"}
        return {**result, "controller_state": controller_state}


def payload_for(directory, plan, item, backend, preparation=None):
    plan = copy.deepcopy(plan)
    plan["jobs"] = [job for job in plan["jobs"] if job["item"] == item]
    keys = {job["key"] for job in plan["jobs"]}
    uploaded = {}
    for job in plan["jobs"]:
        for field in IMAGE_FIELDS:
            binding = job["inputs"].get(field)
            if binding is None:
                continue
            if "job" in binding:
                if binding["job"] not in keys:
                    raise ValueError("Cross-shot dependency is not supported")
            elif binding.get("storage") == "volume":
                ref = image_reference({k: binding[k] for k in ("path", "sha256", "size")}, max_bytes=image_limit(field))
                if ref["path"].startswith(MANAGED_PREFIX):
                    raise ValueError("Import managed inputs through local staging, not a borrowed cloud reference")
                if field == "image" and hasattr(backend, "prepare_edit_original"):
                    ref = sdk.resolve_edit_binding(directory, binding, backend, managed=True)
                job["inputs"][field] = {**ref, "storage": "volume", "type": "image"}
            else:
                digest = binding["sha256"]
                if digest not in uploaded:
                    recorder = local_recorder(state.telemetry_directory(directory),
                        task_id=f"{plan['project']}/{plan['scene']}", job_key=job["key"])
                    with recorder.span("transfer", operation="upload_frames"):
                        uploaded[digest] = backend.upload(directory, binding, managed=True)
                job["inputs"][field] = {**uploaded[digest], "storage": "volume", "type": "image"}
    plan["imports"] = {}
    plan["hash"] = intent_digest({key: value for key, value in plan.items() if key != "hash"})
    current = sdk.snapshot(directory)[1]
    initial = {key: {k: v for k, v in current["jobs"][key].items()
                     if k not in {"local_artifact", "download_error", "local_collected_at", "cloud_archive", "archive_error", "archive_collected_at"}} for key in keys}
    payload = {"version": VERSION, "recovery_schema": 1, "input_lifecycle": LIFECYCLE,
               "plan": plan, "initial_jobs": initial}
    if preparation is not None:
        payload["provisioning"] = preparation
    return payload


def save_receipt(directory, item, **changes):
    with state.lock(directory, timeout=10):
        current = read_json(directory / "state.json")
        current.setdefault("cloud_runs", {}).setdefault(item, {}).update(changes)
        state.save(directory / "state.json", current)


def direct_scene(plan, current):
    jobs = plan["jobs"]
    return (not current.get("cloud_runs") and len(plan["services"]) == 1
            and len({job["item"] for job in jobs}) == len(jobs)
            and not any(isinstance(value, dict) and "job" in value
                        for job in jobs for value in job["inputs"].values()))


def tick_direct(directory, plan, current, job, backend, downloads, pool):
    """Submit a detached GPU call, preserving deployment pinning and replay protection."""
    from pipeline.s03_run_shot import tick

    item, job_key = job["item"], job["key"]
    service = job["uses"].rsplit(".", 1)[0]
    provisioner = getattr(backend, "provisioner", None)
    record = current["jobs"][job_key]
    receipt = current.get("direct_runs", {}).get(item)
    ready = None
    before_submit = None
    try:
        if provisioner is not None and record["status"] in {"pending", "blocked"}:
            pinned = provisioner.batch_keys(plan["services"], plan["hash"])[service]
            key = receipt["preparation"] if receipt else pinned
            ready = provisioner.registry.get(key, {})
            if ready.get("status") == "failed":
                sdk.update(directory, job_key, status="failed",
                           error="Model preparation failed: " + ready.get("error", "unknown"))
                return
            if ready.get("status") != "ready":
                detail = getattr(provisioner, "wait_diagnostics", {}).get(key) or ready.get("status", "preparing")
                sdk.update(directory, job_key, status="blocked", wait_reason="model", wait_detail=detail,
                           model_wait_started_at=record.get("model_wait_started_at") or sdk.utc_now())
                return
            # Freeze every outstanding shot before batch admission. Appending a
            # scene must not strand reserved shots on a newer deployment key.
            with state.lock(directory, timeout=10):
                saved = read_json(directory / "state.json")
                if saved["plan_hash"] != plan["hash"]:
                    return
                runs = saved.setdefault("direct_runs", {})
                changed = False
                for step in plan["jobs"]:
                    if (step["item"] not in runs and
                            saved["jobs"][step["key"]]["status"] not in {"completed", "failed"}):
                        runs[step["item"]] = {
                            "id": "direct:" + run_id(plan, step["item"]),
                            "preparation": pinned,
                        }
                        changed = True
                receipt = runs[item]
                members = {runs[step["item"]]["id"]: [step["key"]] for step in plan["jobs"]
                           if step["item"] in runs
                           and runs[step["item"]]["preparation"] == receipt["preparation"]
                           and saved["jobs"][step["key"]]["status"] not in {"completed", "failed"}}
                if changed:
                    state.save(directory / "state.json", saved)
            if key != receipt["preparation"]:
                key = receipt["preparation"]
                ready = provisioner.registry.get(key, {})
                if ready.get("status") != "ready":
                    return
            if not ready.get("cache_fingerprint"):
                raise RuntimeError("Ready model has no verified cache fingerprint")
            if not provisioner.admit_batch(plan["services"], {service: key}, {service: members}, receipt["id"]):
                sdk.update(directory, job_key, status="blocked", wait_reason="model", wait_detail="Waiting for deployment admission",
                           model_wait_started_at=record.get("model_wait_started_at") or sdk.utc_now())
                return
            if record.get("wait_detail"):
                sdk.update(directory, job_key, wait_detail=None)
            before_submit = lambda intent: provisioner.record_direct(
                key, receipt["id"], job_key, {**intent, "status": "submitting"})

        return tick(directory, backend, downloads, pool, item=item,
                    pinned_preparation=ready, before_submit=before_submit)
    finally:
        # A deterministic terminal outcome releases the admission. The durable
        # GPU result can also prove completion after a local worker crash.
        if provisioner is not None and receipt and not receipt.get("terminal_published"):
            latest = sdk.snapshot(directory)[1]["jobs"][job_key]
            if latest["status"] in {"completed", "failed"}:
                proof = {k: latest[k] for k in ("status", "call_id", "result") if k in latest}
                published = provisioner.record_direct(receipt["preparation"], receipt["id"], job_key, proof)
                if published:
                    with state.lock(directory, timeout=10):
                        saved = read_json(directory / "state.json")
                        saved["direct_runs"][item]["terminal_published"] = True
                        state.save(directory / "state.json", saved)


def tick_shot(directory, item, backend, downloads, pool):
    directory = Path(directory)
    with state.lock(directory / ".runner", filename=".shot-" + intent_digest(item) + ".lock"):
        plan, current = sdk.snapshot(directory)
        jobs = [job for job in plan["jobs"] if job["item"] == item]
        if direct_scene(plan, current) and len(jobs) == 1:
            return tick_direct(directory, plan, current, jobs[0], backend, downloads, pool)
        receipt = current.get("cloud_runs", {}).get(item)
        if receipt is None:
            if all(current["jobs"][job["key"]]["status"] in {"completed", "failed"} for job in jobs):
                return collect_outputs(directory, backend, downloads, pool, item=item)
            provisioner = getattr(backend, "provisioner", None)
            preparation = None
            orchestration_version = None
            if provisioner is not None:
                cloud_key = provisioner.start(batch=plan["hash"])
                preparation = provisioner.batch_keys(plan["services"], plan["hash"])
                readiness = provisioner.registry.get(cloud_key, {})
                if readiness.get("status") == "failed":
                    for job in jobs:
                        if current["jobs"][job["key"]]["status"] in {"pending", "blocked"}:
                            sdk.update(directory, job["key"], status="failed",
                                       error="Cloud preparation failed: " + readiness["error"])
                    return
                if readiness.get("status") != "ready":
                    return
                orchestration_version = readiness.get("version") if readiness.get("app_revision") else None
            for job in jobs:
                if current["jobs"][job["key"]]["status"] not in {"pending", "blocked"}:
                    continue
                kind = sdk.model_capabilities.CAPABILITIES[job["uses"]]["output"]
                stem = state.output_stem(job)
                suffixes = (".png", ".jpg", ".jpeg", ".webp") if kind == "image" else (".mp4",)
                if any((state.media_directory(directory) / (stem + suffix)).exists() for suffix in suffixes):
                    raise FileExistsError("Untracked output; refusing GPU submission")
            identity = run_id(plan, item)
            if preparation:
                groups = {}
                for step in plan["jobs"]:
                    if current["jobs"][step["key"]]["status"] in {"failed", "completed"}:
                        continue
                    service = sdk.model_capabilities.CAPABILITIES[step["uses"]]["service"]
                    members = groups.setdefault(service, {})
                    members.setdefault(run_id(plan, step["item"]), []).append(step["key"])
                if not provisioner.admit_batch(plan["services"], preparation, groups, identity):
                    return
            payload = payload_for(directory, plan, item, backend, preparation)
            backend.persist_workflow(payload, identity)
            receipt = {"id": identity, "payload_hash": intent_digest(payload), "status": "submitting"}
            if preparation:
                receipt["provisioning"] = preparation
            save_receipt(directory, item, **receipt)
            try:
                recorder = local_recorder(state.telemetry_directory(directory), task_id=f"{plan['project']}/{plan['scene']}")
                with recorder.span("submit", operation="workflow", item=item) as fields:
                    pin = {"deployment_version": orchestration_version} if orchestration_version is not None else {}
                    call_id = backend.submit_workflow(payload, receipt["id"], persisted=True, **pin)
                    fields["call_id"] = call_id
                save_receipt(directory, item, status="queued", call_id=call_id)
            except BaseException:
                save_receipt(directory, item, status="uncertain", error="Workflow acceptance unknown; never resubmit")
                raise
        else:
            if receipt["status"] == "submitting":
                save_receipt(directory, item, status="uncertain")
            if receipt["status"] not in {"completed", "failed"}:
                remote = backend.poll_workflow(receipt)
                if remote is not None:
                    if set(remote["jobs"]) != {job["key"] for job in jobs}:
                        raise ValueError("Unexpected cloud job identities")
                    with state.lock(directory, timeout=10):
                        current = read_json(directory / "state.json")
                        for key, record in remote["jobs"].items():
                            # Local downloads are a separate lifecycle.
                            local = {k: v for k, v in current["jobs"][key].items()
                                     if k in {"local_artifact", "download_error", "local_collected_at", "cloud_archive", "archive_error", "archive_collected_at"}}
                            merged = {**record, **local}
                            if current["jobs"][key] != merged:
                                state.record_event(directory, key, merged)
                            current["jobs"][key] = merged
                        current["cloud_runs"][item].update(
                            status=remote["status"], telemetry=remote.get("telemetry"),
                            controller_state=remote.get("controller_state"), error=remote.get("error"))
                        state.save(directory / "state.json", current)
        # Re-discover manifests from durable state, including terminal jobs after restart.
        _, current = sdk.snapshot(directory)
        task_id = f"{plan['project']}/{plan['scene']}"
        manifests = [current.get("cloud_runs", {}).get(item, {}).get("telemetry")]
        manifests.extend(current["jobs"][job["key"]].get("result", {}).get("telemetry") for job in jobs)
        for manifest in manifests:
            if manifest:
                enqueue(state.telemetry_directory(directory), backend.volume, {**manifest, "task_id": task_id})
        return collect_outputs(directory, backend, downloads, pool, item=item)


def worker(output, *, poll=2, backend=None, stop=None, task_name=None):
    if not math.isfinite(poll) or poll <= 0:
        raise ValueError("poll must be positive")
    if backend is None:
        backend = Backend()
        from orchestration.provisioning import Provisioner
        backend.provisioner = Provisioner(backend)
    active, downloads, scheduled = {}, {}, {}
    from runtime.telemetry.collector import start
    runtime = state.runtime_root(output)
    start(runtime / "logs", backend.volume)
    from orchestration.monitor import render
    last_render = 0
    # A slow network call occupies one delivery slot, never the scan loop or downloads.
    with ExitStack() as lifecycle, ThreadPoolExecutor(max_workers=8) as network, ThreadPoolExecutor(max_workers=8) as pool:
        if getattr(backend, "provisioner", None) is not None:
            lifecycle.callback(backend.provisioner.close)
        while stop is None or not stop.is_set():
            if time.monotonic() - last_render >= 5:
                try:
                    render(output)
                except (OSError, ValueError, KeyError) as exc:
                    print(f"Monitor: {exc}", flush=True)
                last_render = time.monotonic()
            for key, future in list(active.items()):
                if future.done():
                    try:
                        future.result()
                    except Exception as exc:
                        print(f"{key}: {type(exc).__name__}: {exc}", flush=True)
                    del active[key]
            candidates = []
            for path in sorted((runtime / "tasks").glob("*/*/plan.json")):
                try:
                    plan = read_json(path)
                    if plan.get("transport") != "sdk":
                        continue
                    if task_name is not None and plan.get("name") != task_name:
                        continue
                    for item in dict.fromkeys(job["item"] for job in plan["jobs"]):
                        key = (str(path.parent), item)
                        if key not in active:
                            candidates.append(key)
                except (ValueError, OSError, KeyError) as exc:
                    print(f"{path}: {type(exc).__name__}", flush=True)
            for key in sorted(candidates, key=lambda key: scheduled.get(key, 0))[:max(0, 8 - len(active))]:
                active[key] = network.submit(tick_shot, key[0], key[1], backend,
                                             downloads.setdefault(key, {}), pool)
                scheduled[key] = time.monotonic()
            if stop is None:
                time.sleep(poll)
            else:
                stop.wait(poll)
