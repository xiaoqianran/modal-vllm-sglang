"""Dependency scheduling over detached Modal calls and committed cloud artifacts.

Scene locks protect only JSON transactions. Per-job locks protect a single spawn;
downloads run separately and never determine whether a dependency is ready.
"""
from concurrent.futures import ThreadPoolExecutor
import math
from pathlib import Path
import time

from orchestration import adapters, state
from shared.tasks.batch import intent_digest
from shared.tasks.task_spec import prepare, result_prefix, execution_signature
from shared.tasks.job_client import remote_cache_fingerprint, remote_record, check_record
from shared.tasks.artifacts import image_reference
from shared.utils.files import download_volume_file, read_json, sha256_file


def model_specs():
    from shared.config.model_spec import ROOT, validate
    return {spec["service"]: spec for path in ROOT.glob("*/model.json")
            for spec in [validate(read_json(path))]}


def check_deployment(spec, *, dry_run=False):
    """A separate diagnostic, never a generation submission mode."""
    if dry_run:
        return {"mode": "check_deployment", "service": spec["service"], "starts_gpu": True}
    from shared.config.storage import VOLUME_NAME
    backend = Backend()
    fingerprint = backend.fingerprint(spec)
    model = backend.modal.Cls.from_name(spec.get("app_name", "modal-model-" + spec["service"]),
                                        spec.get("modal_class", "Model"))()
    started = time.perf_counter()
    info = model.deployment_info.remote()
    if (info.get("spec_hash") != intent_digest(spec) or info.get("cache_fingerprint") != fingerprint
            or "generate_one" not in info.get("methods", []) or info.get("cloud_artifacts") != 1
            or info.get("volume") != VOLUME_NAME):
        raise RuntimeError("Deployment contract mismatch; prepare cache and redeploy current code")
    return {**info, "request_seconds": time.perf_counter() - started}


def request_resume(directory):
    """Wake collection in a running worker; never spawn or wait for generation."""
    directory = Path(directory)
    snapshot(directory)
    with state.lock(directory, timeout=10):
        current = read_json(directory / "state.json")
        current["collection_epoch"] = current.get("collection_epoch", 0) + 1
        state.save(directory / "state.json", current)


class Backend:
    def __init__(self):
        import modal
        from shared.serving.modal_setup import model_volume
        self.modal = modal
        self.volume = model_volume()
        self.models = {}
        self.fingerprints = {}

    def fingerprint(self, spec):
        key = intent_digest(spec)
        if key not in self.fingerprints:
            self.fingerprints[key] = remote_cache_fingerprint(spec, self.volume)
        return self.fingerprints[key]

    def upload(self, directory, artifact):
        path = state.local_path(directory, artifact["path"])
        if not state.valid(directory, artifact):
            raise ValueError("Frozen input image changed")
        ref = image_reference({"path": "workflow-inputs/" + artifact["sha256"],
                               "sha256": artifact["sha256"], "size": artifact["size"]})
        with self.volume.batch_upload(force=True) as batch:
            batch.put_file(str(path), "/" + ref["path"])
        return ref

    def submit(self, spec, item, prefix, fingerprint):
        key = intent_digest(spec)
        if key not in self.models:
            self.models[key] = self.modal.Cls.from_name(spec.get("app_name", "modal-model-" + spec["service"]), spec.get("modal_class", "Model"))()
        return self.models[key].generate_one.spawn(item, prefix, fingerprint, key).object_id

    def poll(self, record):
        # Committed metadata outlives Modal call result retention.
        result = remote_record(self.volume, record["prefix"], record["id"])
        if result:
            check_record(record, result)
            if result.get("status") in {"completed", "failed", "cancelled"}:
                return result
        if not record.get("call_id"):
            return None
        try:
            result = self.modal.FunctionCall.from_id(record["call_id"]).get(timeout=0)
        except TimeoutError:
            return None
        check_record(record, result)
        if result.get("status") != "completed":
            raise ValueError("Cloud call returned an incomplete result")
        return result

    def download(self, directory, job, record, plan):
        kind = adapters.CAPABILITIES[job["uses"]]["output"]
        siblings = sum(other["item"] == job["item"]
                       and adapters.CAPABILITIES[other["uses"]]["output"] == kind
                       for other in plan["jobs"])
        stem = job["item"] if siblings == 1 else f"{job['item']}-{job['step']}"
        suffix = Path(record["result"]["file"]).suffix
        target = directory / (stem + suffix)
        artifact = record["artifacts"][kind]
        with state.lock(directory, filename=".download-" + intent_digest(job["key"]) + ".lock"):
            if target.exists():
                if sha256_file(target) != artifact["sha256"]:
                    raise FileExistsError("Local output differs from cloud artifact")
            else:
                download_volume_file(self.volume, "/" + artifact["path"], target,
                                     expected_sha256=artifact["sha256"], kind=kind, refuse_existing=True)
        return {**artifact, "storage": "local", "path": target.relative_to(directory).as_posix()}


def snapshot(directory):
    with state.lock(directory, timeout=10):
        plan = read_json(directory / "plan.json")
        current = read_json(directory / "state.json")
        from shared.config.storage import VOLUME_NAME
        if (plan.get("transport") != "sdk" or plan.get("volume") != VOLUME_NAME
                or plan.get("adapter_version") != adapters.VERSION
                or intent_digest({k: v for k, v in plan.items() if k != "hash"}) != plan["hash"]
                or current["plan_hash"] != plan["hash"]):
            raise ValueError("SDK plan/storage changed; refusing execution")
        from orchestration.runner import bindings
        if current["endpoints"] != {k: v["endpoint"] for k, v in bindings(plan).items()}:
            raise ValueError("SDK deployment binding changed")
        return plan, current


def update(directory, key, **changes):
    with state.lock(directory, timeout=10):
        current = read_json(directory / "state.json")
        current["jobs"][key].update(changes)
        state.save(directory / "state.json", current)


def cloud_artifact(record, kind):
    result = record["result"]
    return {"type": kind, "storage": "volume", "path": record["prefix"] + "/" + result["file"],
            "sha256": result["sha256"], "size": result["bytes"]}


def item_for(directory, job, records, spec, backend):
    inputs = dict(job["inputs"])
    item = {"id": "j" + intent_digest(job["key"])[:32],
            "operation": job["uses"].split(".")[1],
            "prompt": inputs["prompt"], "seed": inputs["seed"],
            "params": {key: job["params"][key] for key in spec["options"] if key in job["params"]}}
    for field in ("first_frame", "last_frame"):
        if field not in inputs:
            continue
        binding = inputs[field]
        if "job" in binding:
            upstream = records[binding["job"]]
            if upstream["status"] != "completed":
                return None
            artifact = upstream["artifacts"][binding["output"]]
            ref = image_reference({key: artifact[key] for key in ("path", "sha256", "size")})
        else:
            ref = backend.upload(directory, binding)
        item[field + "_artifact"] = ref
    return item


def tick(directory, backend, downloads, pool):
    """Advance independent jobs once; never wait for inference or local downloads."""
    directory = Path(directory)
    plan, current = snapshot(directory)
    # Pair a shot's first/last images in the cloud queue to unlock its video early.
    order = {job["key"]: index for index, job in enumerate(plan["jobs"])}
    jobs = sorted(plan["jobs"], key=lambda job: (job["item"], order[job["key"]]))
    for job in jobs:
        key = job["key"]
        try:
            with state.lock(directory / ".runner", filename=".job-" + intent_digest(key) + ".lock"):
                # Another process or an append may have changed the scene.
                _, current = snapshot(directory)
                records = current["jobs"]
                record = records[key]
                kind = adapters.CAPABILITIES[job["uses"]]["output"]
                if record["status"] == "submitting":
                    update(directory, key, status="uncertain", error="Submission interrupted; never automatically resubmit")
                    continue
                if (record.get("call_id") or record["status"] == "uncertain" and record.get("prefix")) and record["status"] not in {"completed", "failed"}:
                    try:
                        result = backend.poll(record)
                        if result is not None:
                            if result["status"] != "completed":
                                update(directory, key, status="failed", error="Cloud generation failed")
                            else:
                                artifact = cloud_artifact({**record, "result": result}, kind)
                                update(directory, key, status="completed", result=result,
                                       artifacts={kind: artifact}, error=None)
                    except Exception as exc:
                        update(directory, key, status="uncertain", error=type(exc).__name__ + ": query failed; retained call ID")
                    continue
                if record["status"] not in {"pending", "blocked"}:
                    continue
                service = adapters.CAPABILITIES[job["uses"]]["service"]
                spec = plan["services"][service]["spec"]
                # Dependencies, not downloaded local files, determine readiness.
                dependencies = [value["job"] for value in job["inputs"].values()
                                if isinstance(value, dict) and "job" in value]
                if any(records[dep]["status"] != "completed" for dep in dependencies):
                    if record["status"] != "blocked":
                        update(directory, key, status="blocked")
                    continue
                siblings = sum(other["item"] == job["item"]
                               and adapters.CAPABILITIES[other["uses"]]["output"] == kind
                               for other in plan["jobs"])
                stem = job["item"] if siblings == 1 else f"{job['item']}-{job['step']}"
                suffixes = (".png", ".jpg", ".jpeg", ".webp") if kind == "image" else (".mp4",)
                if any((directory / (stem + suffix)).exists() for suffix in suffixes):
                    update(directory, key, status="failed", error="Untracked local output; refusing GPU submission. Import frames explicitly instead.")
                    continue
                item = item_for(directory, job, records, spec, backend)
                signature = prepare(spec, item)[1]
                fingerprint = backend.fingerprint(spec)
                prefix = result_prefix(spec, plan["project"], plan["scene"], item["operation"])
                receipt = {"id": item["id"], "prefix": prefix, "intent_hash": signature,
                           "cache_fingerprint": fingerprint,
                           "execution_hash": execution_signature(signature, fingerprint)}
                # Durable intent precedes the one and only spawn attempt.
                update(directory, key, **receipt, status="submitting")
                try:
                    call_id = backend.submit(spec, item, prefix, fingerprint)
                    update(directory, key, status="queued", call_id=call_id)
                except BaseException:
                    update(directory, key, status="uncertain", error="Spawn acceptance unknown; never automatically resubmit")
                    raise
        except RuntimeError as exc:
            if not str(exc).startswith("Another runner holds "):
                raise
            # Only this job is occupied. Other jobs/scenes keep progressing.
            continue
    plan, current = snapshot(directory)
    for job in plan["jobs"]:
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
            update(directory, key, local_artifact=future.result(), download_error=None)
        except Exception as exc:
            update(directory, key, local_artifact=None,
                   download_error=type(exc).__name__ + ": local collection failed; cloud output remains ready")
        download["reported"] = True
    plan, current = snapshot(directory)
    from orchestration.runner import report
    result = report(plan, current["jobs"])
    with state.lock(directory, timeout=10):
        # Reports are derived snapshots. Do not overwrite a newer appended scene's report.
        if read_json(directory / "state.json")["plan_hash"] == plan["hash"]:
            state.save(directory / "report.json", result)
    return result


def execute(directory, *, backend=None, timeout=3600, poll=2):
    return execute_many([directory], backend=backend, timeout=timeout, poll=poll)[0]


def execute_many(directories, *, backend=None, timeout=3600, poll=2):
    if not all(math.isfinite(v) and v > 0 for v in (timeout, poll)):
        raise ValueError("timeout and poll must be positive")
    backend = backend or Backend()
    downloads = {}
    deadline = time.monotonic() + timeout
    with ThreadPoolExecutor(max_workers=8) as pool:
        while True:
            results = [tick(directory, backend, downloads, pool) for directory in directories]
            active = any(r["status"] in {"pending", "queued", "submitting"}
                         or (r["status"] == "uncertain" and r.get("call_id"))
                         for directory in directories for r in snapshot(directory)[1]["jobs"].values())
            collecting = any(not download["future"].done() for download in downloads.values())
            if (not active and not collecting) or time.monotonic() >= deadline:
                # One final tick commits any just-finished download results.
                return [tick(directory, backend, downloads, pool) for directory in directories]
            time.sleep(min(poll, max(0, deadline - time.monotonic())))


def worker(output, *, poll=2, backend=None):
    """One local coordinator sees new scenes and appended shots on every pass."""
    if not math.isfinite(poll) or poll <= 0:
        raise ValueError("poll must be positive")
    backend = backend or Backend()
    downloads = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        while True:
            for path in sorted(Path(output).glob("*/*/plan.json")):
                if read_json(path).get("transport") != "sdk":
                    continue
                try:
                    tick(path.parent, backend, downloads, pool)
                except (ValueError, OSError, RuntimeError, KeyError) as exc:
                    print(f"{path.parent}: {type(exc).__name__}; scene will be checked again", flush=True)
            time.sleep(poll)
