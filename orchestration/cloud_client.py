"""Bounded local delivery and collection; dependency scheduling lives in Modal."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import math
from pathlib import Path
import time

from orchestration import sdk, state
from orchestration.protocol import APP_NAME, VERSION, run_id, state_path
from shared.tasks.batch import intent_digest
from shared.telemetry.collector import enqueue
from shared.telemetry.recorder import local_recorder
from shared.utils.files import read_json


class Backend(sdk.Backend):
    def submit_workflow(self, payload, identity):
        return self.modal.Function.from_name(APP_NAME, "run_shot").spawn(payload, identity).object_id

    def poll_workflow(self, receipt):
        try:
            data = bytearray()
            for chunk in self.volume.read_file("/" + state_path(receipt["id"])):
                data.extend(chunk)
                if len(data) > 16 * 1024 * 1024:
                    raise ValueError("Workflow state too large")
            result = json.loads(data)
        except FileNotFoundError:
            if not receipt.get("call_id"):
                return None
            try:
                result = self.modal.FunctionCall.from_id(receipt["call_id"]).get(timeout=0)
            except TimeoutError:
                return None
        if result.get("version") != VERSION or result.get("payload_hash") != receipt["payload_hash"]:
            raise ValueError("Workflow receipt/configuration mismatch")
        return result


def payload_for(directory, plan, item, backend):
    plan = copy.deepcopy(plan)
    plan["jobs"] = [job for job in plan["jobs"] if job["item"] == item]
    keys = {job["key"] for job in plan["jobs"]}
    uploaded = {}
    for job in plan["jobs"]:
        for field in ("first_frame", "last_frame"):
            binding = job["inputs"].get(field)
            if binding is None:
                continue
            if "job" in binding:
                if binding["job"] not in keys:
                    raise ValueError("Cross-shot dependency is not supported")
            else:
                digest = binding["sha256"]
                if digest not in uploaded:
                    recorder = local_recorder(directory.parent.parent,
                        task_id=f"{plan['project']}/{plan['scene']}", job_key=job["key"])
                    with recorder.span("transfer", operation="upload_frames"):
                        uploaded[digest] = backend.upload(directory, binding)
                job["inputs"][field] = {**uploaded[digest], "storage": "volume", "type": "image"}
    plan["imports"] = {}
    plan["hash"] = intent_digest({key: value for key, value in plan.items() if key != "hash"})
    current = sdk.snapshot(directory)[1]
    initial = {key: {k: v for k, v in current["jobs"][key].items()
                     if k not in {"local_artifact", "download_error"}} for key in keys}
    return {"version": VERSION, "plan": plan, "initial_jobs": initial}


def save_receipt(directory, item, **changes):
    with state.lock(directory, timeout=10):
        current = read_json(directory / "state.json")
        current.setdefault("cloud_runs", {}).setdefault(item, {}).update(changes)
        state.save(directory / "state.json", current)


def tick_shot(directory, item, backend, downloads, pool):
    directory = Path(directory)
    with state.lock(directory / ".runner", filename=".shot-" + intent_digest(item) + ".lock"):
        plan, current = sdk.snapshot(directory)
        jobs = [job for job in plan["jobs"] if job["item"] == item]
        receipt = current.get("cloud_runs", {}).get(item)
        if receipt is None:
            for job in jobs:
                if current["jobs"][job["key"]]["status"] not in {"pending", "blocked"}:
                    continue
                kind = sdk.adapters.CAPABILITIES[job["uses"]]["output"]
                siblings = sum(sdk.adapters.CAPABILITIES[j["uses"]]["output"] == kind for j in jobs)
                stem = item if siblings == 1 else f"{item}-{job['step']}"
                suffixes = (".png", ".jpg", ".jpeg", ".webp") if kind == "image" else (".mp4",)
                if any((directory / (stem + suffix)).exists() for suffix in suffixes):
                    raise FileExistsError("Untracked output; refusing GPU submission")
            payload = payload_for(directory, plan, item, backend)
            receipt = {"id": run_id(plan, item), "payload_hash": intent_digest(payload), "status": "submitting"}
            save_receipt(directory, item, **receipt)
            try:
                recorder = local_recorder(directory.parent.parent, task_id=f"{plan['project']}/{plan['scene']}")
                with recorder.span("submit", operation="workflow", item=item) as fields:
                    call_id = backend.submit_workflow(payload, receipt["id"])
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
                                     if k in {"local_artifact", "download_error"}}
                            current["jobs"][key] = {**record, **local}
                        current["cloud_runs"][item].update(status=remote["status"], telemetry=remote.get("telemetry"))
                        state.save(directory / "state.json", current)
        # Re-discover manifests from durable state, including terminal jobs after restart.
        _, current = sdk.snapshot(directory)
        task_id = f"{plan['project']}/{plan['scene']}"
        manifests = [current.get("cloud_runs", {}).get(item, {}).get("telemetry")]
        manifests.extend(current["jobs"][job["key"]].get("result", {}).get("telemetry") for job in jobs)
        for manifest in manifests:
            if manifest:
                enqueue(directory.parent.parent, backend.volume, {**manifest, "task_id": task_id})
        return sdk.collect_outputs(directory, backend, downloads, pool, item=item)


def worker(output, *, poll=2, backend=None, stop=None):
    if not math.isfinite(poll) or poll <= 0:
        raise ValueError("poll must be positive")
    backend = backend or Backend()
    active, downloads, scheduled = {}, {}, {}
    from shared.telemetry.collector import start
    start(output, backend.volume)
    # A slow network call occupies one delivery slot, never the scan loop or downloads.
    with ThreadPoolExecutor(max_workers=8) as network, ThreadPoolExecutor(max_workers=8) as pool:
        while stop is None or not stop.is_set():
            for key, future in list(active.items()):
                if future.done():
                    try:
                        future.result()
                    except Exception as exc:
                        print(f"{key}: {type(exc).__name__}: {exc}", flush=True)
                    del active[key]
            candidates = []
            for path in sorted(Path(output).glob("*/*/plan.json")):
                try:
                    plan = read_json(path)
                    if plan.get("transport") != "sdk":
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
