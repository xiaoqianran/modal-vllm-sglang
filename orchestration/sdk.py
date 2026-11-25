"""Dependency scheduling over detached Modal calls and committed cloud artifacts.

Scene locks protect only JSON transactions. Per-job locks protect a single spawn;
downloads run separately and never determine whether a dependency is ready.
"""
from pipeline.s04_collect_outputs import collect_outputs
from pathlib import Path
import time

from orchestration import model_capabilities, state
from orchestration.sdk_session import SDKSession
from orchestration.input_store import stage_input
from runtime.tasks.task_identity import intent_digest
from runtime.tasks.task_spec import prepare, result_prefix, execution_signature
from runtime.tasks.job_client import remote_cache_fingerprint, remote_record, check_record
from runtime.tasks.artifacts import image_reference
from runtime.utils.files import read_json
from runtime.telemetry.recorder import local_recorder
from runtime.telemetry.schema import utc_now


# Keep committed evidence discoverable while a known call is pending;
# avoid a redundant Volume read on every 2-second scheduler tick.
_LEDGER_INTERVAL = 6


def model_specs():
    from runtime.config.model_spec import ROOT, validate
    return {spec["service"]: spec for path in ROOT.glob("models/*/model.json")
            for spec in [validate(read_json(path))]}


def check_deployment(spec, *, dry_run=False):
    """A separate diagnostic, never a generation submission mode."""
    if dry_run:
        return {"mode": "check_deployment", "service": spec["service"], "starts_gpu": True}
    from runtime.config.storage import runtime_volume_name
    backend = Backend()
    fingerprint = backend.fingerprint(spec)
    model = backend.modal.Cls.from_name(spec.get("app_name", "modal-model-" + spec["service"]),
                                        spec.get("modal_class", "Model"))()
    started = time.perf_counter()
    info = model.deployment_info.remote()
    if (info.get("spec_hash") != intent_digest(spec) or info.get("cache_fingerprint") != fingerprint
            or "generate_one" not in info.get("methods", []) or info.get("cloud_artifacts") != 1
            or info.get("telemetry_schema_version") != 1
            or info.get("volume") != runtime_volume_name()):
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
        from runtime.serving.modal_setup import model_volume
        self.modal = modal
        self.volume = model_volume()
        self.models = {}
        self.fingerprints = {}
        self.telemetry_manifests = {}
        self.session = SDKSession()

    def fingerprint(self, spec):
        key = intent_digest(spec)
        with self.session.keyed_lock(("fingerprint", key)):
            if key not in self.fingerprints:
                self.fingerprints[key] = remote_cache_fingerprint(spec, self.volume)
            return self.fingerprints[key]

    def upload(self, directory, artifact):
        return stage_input(self.volume, self.session, directory, artifact)

    def submit(self, spec, item, prefix, fingerprint, *, deployment_version=None):
        key = intent_digest(spec)
        handle_key = (key, deployment_version)
        with self.session.keyed_lock(("model", handle_key)):
            if handle_key not in self.models:
                self.models[handle_key] = self.modal.Cls.from_name(
                    spec.get("app_name", "modal-model-" + spec["service"]),
                    spec.get("modal_class", "Model"))()
        execution_hash = execution_signature(prepare(spec, item)[1], fingerprint)
        # A stable class tag can still receive an old rolling container.
        # Guard the invocation itself, without paid version-pinned lookup.
        guard = {"expected_deployment_version": deployment_version} if deployment_version is not None else {}
        return self.submission_ledger().submit(prefix, item["id"], execution_hash,
            lambda: self.models[handle_key].generate_one.spawn(item, prefix, fingerprint, key, **guard).object_id)

    def submission_ledger(self):
        from orchestration.protocol import SUBMISSION_REGISTRY
        from orchestration.submissions import SubmissionLedger
        with self.session.lock:
            if not hasattr(self, "_submission_ledger"):
                self._submission_ledger = SubmissionLedger(self.modal.Dict.from_name(SUBMISSION_REGISTRY, create_if_missing=True))
            return self._submission_ledger

    def call_handle(self, call_id):
        return self.session.handle(("call", call_id), lambda: self.modal.FunctionCall.from_id(call_id))

    def _committed_result(self, record):
        result = remote_record(self.volume, record["prefix"], record["id"])
        if result:
            check_record(record, result)
            if result.get("telemetry"):
                self.telemetry_manifests[record.get("call_id")] = result["telemetry"]
            if result.get("status") in {"completed", "failed", "cancelled"}:
                return result
        return None

    def poll(self, record):
        # The Volume ledger remains authoritative. Check on first observation,
        # every bounded interval while pending, and immediately on call exit
        # or error. Unknown acceptance always checks the ledger first.
        call_id = record.get("call_id")
        identity = (record["prefix"], record["id"], record["execution_hash"], call_id)
        if not call_id or record.get("status") == "uncertain" or self.session.ledger_due(identity, _LEDGER_INTERVAL):
            committed = self._committed_result(record)
            if committed is not None:
                self.session.forget_ledger(identity)
                if call_id:
                    self.session.forget_handle(("call", call_id))
                return committed
            self.session.note_ledger_read(identity)
        if not call_id and record.get("prefix") and record.get("id"):
            entry = self.submission_ledger().lookup(record["prefix"], record["id"], record["execution_hash"])
            if entry and entry.get("call_id"):
                call_id = entry["call_id"]
                record = {**record, "call_id": call_id}
        if not call_id:
            return None
        from modal.exception import TimeoutError as ModalTimeoutError
        try:
            result = self.call_handle(call_id).get(timeout=0)
        except (TimeoutError, ModalTimeoutError) as exc:
            if type(exc) in (TimeoutError, ModalTimeoutError):
                return None
            self.session.forget_handle(("call", call_id))
            self.session.forget_ledger(identity)
            committed = self._committed_result(record)
            if committed is not None:
                return committed
            raise
        except Exception:
            self.session.forget_handle(("call", call_id))
            self.session.forget_ledger(identity)
            committed = self._committed_result(record)
            if committed is not None:
                return committed
            raise
        self.session.forget_handle(("call", call_id))
        self.session.forget_ledger(identity)
        committed = self._committed_result(record)
        if committed is not None:
            return committed
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
        from runtime.delivery.local import download_to_local
        return download_to_local(self.volume, directory, job, record, fields)

    def archive(self, directory, job, record, plan):
        from runtime.delivery.huggingface import archive_from_volume
        return archive_from_volume(self.modal, job, record, plan)


def snapshot(directory):
    with state.lock(directory, timeout=10):
        plan = read_json(directory / "plan.json")
        current = read_json(directory / "state.json")
        from runtime.config.storage import runtime_volume_name, VOLUME_SUB_PATH
        if (plan.get("transport") != "sdk" or plan.get("volume") != runtime_volume_name()
                or plan.get("volume_sub_path") != VOLUME_SUB_PATH
                or plan.get("adapter_version") != model_capabilities.VERSION
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
