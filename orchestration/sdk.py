"""Dependency scheduling over detached Modal calls and committed cloud artifacts.

Scene locks protect only JSON transactions. Per-job locks protect a single spawn;
downloads run separately and never determine whether a dependency is ready.
"""
from pathlib import Path
import time

from orchestration import adapters, state
from shared.tasks.batch import intent_digest
from shared.tasks.task_spec import prepare, result_prefix, execution_signature
from shared.tasks.job_client import remote_cache_fingerprint, remote_record, check_record
from shared.tasks.artifacts import image_reference
from shared.utils.files import download_volume_file, read_json, sha256_file
from shared.telemetry.recorder import local_recorder
from shared.telemetry.schema import utc_now


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
            or info.get("telemetry_schema_version") != 1
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
        self.telemetry_manifests = {}

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
        execution_hash = execution_signature(prepare(spec, item)[1], fingerprint)
        return self.submission_ledger().submit(prefix, item["id"], execution_hash,
            lambda: self.models[key].generate_one.spawn(item, prefix, fingerprint, key).object_id)

    def submission_ledger(self):
        from orchestration.protocol import SUBMISSION_REGISTRY
        from orchestration.submissions import SubmissionLedger
        if not hasattr(self, "_submission_ledger"):
            self._submission_ledger = SubmissionLedger(self.modal.Dict.from_name(SUBMISSION_REGISTRY, create_if_missing=True))
        return self._submission_ledger

    def poll(self, record):
        # Committed metadata outlives Modal call result retention.
        result = remote_record(self.volume, record["prefix"], record["id"])
        if result:
            check_record(record, result)
            if result.get("telemetry"):
                self.telemetry_manifests[record.get("call_id")] = result["telemetry"]
            if result.get("status") in {"completed", "failed", "cancelled"}:
                return result
        if not record.get("call_id") and record.get("prefix") and record.get("id"):
            entry = self.submission_ledger().lookup(record["prefix"], record["id"], record["execution_hash"])
            if entry and entry.get("call_id"):
                record = {**record, "call_id": entry["call_id"]}
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
        recorder = local_recorder(state.telemetry_directory(directory), task_id=f"{plan['project']}/{plan['scene']}",
                                  job_key=job["key"], call_id=record.get("call_id"))
        with recorder.span("download") as fields:
            return self._download(directory, job, record, plan, fields)

    def _download(self, directory, job, record, plan, fields):
        kind = adapters.CAPABILITIES[job["uses"]]["output"]
        stem = state.output_stem(job)
        suffix = Path(record["result"]["file"]).suffix
        target = state.media_directory(directory) / (stem + suffix)
        artifact = record["artifacts"][kind]
        with state.lock(directory, filename=".download-" + intent_digest(job["key"]) + ".lock"):
            if target.exists():
                if sha256_file(target) != artifact["sha256"]:
                    raise FileExistsError("Local output differs from cloud artifact")
                fields["reused"] = True
            else:
                fields.update(download_volume_file(self.volume, "/" + artifact["path"], target,
                              expected_sha256=artifact["sha256"], kind=kind, refuse_existing=True))
        return {**artifact, "storage": "local", "path": target.relative_to(state.media_directory(directory)).as_posix()}


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
        from orchestration.protocol import bindings
        if current["endpoints"] != {k: v["endpoint"] for k, v in bindings(plan).items()}:
            raise ValueError("SDK deployment binding changed")
        return plan, current


def update(directory, key, *, checkpoint=None, **changes):
    with state.lock(directory, timeout=10):
        current = read_json(directory / "state.json")
        record = current["jobs"][key]
        if changes.get("status") in {"completed", "failed"} and "finished_at" not in record:
            changes.setdefault("finished_at", utc_now())
        changed = any(field not in record or record[field] != value for field, value in changes.items())
        if changed:
            record.update(changes)
            state.save(directory / "state.json", current)
    if changed:
        state.record_event(directory, key, changes)
    if checkpoint is not None:
        checkpoint(directory)


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
    item["_telemetry"] = {"task_id": f"{directory.parent.name}/{directory.name}", "job_key": job["key"]}
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
        elif binding.get("storage") == "volume":
            ref = image_reference({key: binding[key] for key in ("path", "sha256", "size")})
        else:
            recorder = local_recorder(state.telemetry_directory(directory), **item["_telemetry"])
            with recorder.span("transfer", operation="upload_frames"):
                ref = backend.upload(directory, binding)
        item[field + "_artifact"] = ref
    return item


def tick(directory, backend, downloads, pool):
    """Advance independent jobs once; never wait for inference or local downloads."""
    directory = Path(directory)
    plan, current = snapshot(directory)
    def advance(key, **changes):
        update(directory, key, checkpoint=getattr(backend, "checkpoint", None), **changes)
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
                recorder = (backend.recorder.bind(task_id=f"{plan['project']}/{plan['scene']}", job_key=key, call_id=record.get("call_id"))
                            if getattr(backend, "recorder", None) is not None else
                            local_recorder(state.telemetry_directory(directory), task_id=f"{plan['project']}/{plan['scene']}", job_key=key, call_id=record.get("call_id")))
                kind = adapters.CAPABILITIES[job["uses"]]["output"]
                if record["status"] == "submitting":
                    advance(key, status="uncertain", error="Submission interrupted; never automatically resubmit")
                    continue
                if (record.get("call_id") or record["status"] == "uncertain" and record.get("prefix")) and record["status"] not in {"completed", "failed"}:
                    result = None
                    try:
                        result = backend.poll(record)
                        if result is not None:
                            if result["status"] != "completed":
                                advance(key, status="failed", result=result, error="Cloud generation failed")
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
                        ):
                            advance(key, status="failed", error=str(exc))
                        else:
                            advance(key, status="uncertain", error=type(exc).__name__ + ": query failed; retained call ID")
                    finally:
                        from shared.telemetry.collector import enqueue
                        manifest = getattr(backend, "telemetry_manifests", {}).pop(record.get("call_id"), {})
                        if not manifest and isinstance(result, dict):
                            manifest = result.get("telemetry", {})
                        if getattr(backend, "collect_outputs", True):
                            enqueue(state.telemetry_directory(directory), backend.volume,
                                {**manifest, "task_id": f"{plan['project']}/{plan['scene']}"})
                    continue
                if record["status"] not in {"pending", "blocked"}:
                    continue
                service = adapters.CAPABILITIES[job["uses"]]["service"]
                spec = plan["services"][service]["spec"]
                # Dependencies, not downloaded local files, determine readiness.
                dependencies = [value["job"] for value in job["inputs"].values()
                                if isinstance(value, dict) and "job" in value]
                if any(records[dep]["status"] == "failed" for dep in dependencies):
                    advance(key, status="failed", error="Dependency failed")
                    continue
                readiness = getattr(backend, "model_readiness", None)
                if readiness is not None:
                    preparation = readiness(service)
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
                siblings = sum(other["item"] == job["item"]
                               and adapters.CAPABILITIES[other["uses"]]["output"] == kind
                               for other in plan["jobs"])
                stem = state.output_stem(job)
                suffixes = (".png", ".jpg", ".jpeg", ".webp") if kind == "image" else (".mp4",)
                if any((state.media_directory(directory) / (stem + suffix)).exists() for suffix in suffixes):
                    advance(key, status="failed", error="Untracked local output; refusing GPU submission. Import frames explicitly instead.")
                    continue
                item = item_for(directory, job, records, spec, backend)
                signature = prepare(spec, item)[1]
                fingerprint = backend.fingerprint(spec)
                prefix = result_prefix(spec, plan["project"], plan["scene"], item["operation"])
                receipt = {"id": item["id"], "prefix": prefix, "intent_hash": signature,
                           "cache_fingerprint": fingerprint,
                           "execution_hash": execution_signature(signature, fingerprint)}
                # Durable intent precedes the one and only spawn attempt.
                advance(key, **receipt, status="submitting", wait_reason=None)
                try:
                    with recorder.span("submit") as fields:
                        call_id = backend.submit(spec, item, prefix, fingerprint)
                        fields["call_id"] = call_id
                    advance(key, status="queued", call_id=call_id, submitted_at=utc_now(),
                           dependency_wait_started_at=None)
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
    return collect_outputs(directory, backend, downloads, pool)


def collect_outputs(directory, backend, downloads, pool, *, item=None):
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
