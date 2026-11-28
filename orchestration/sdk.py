"""Dependency scheduling over detached Modal calls and committed cloud artifacts.

Scene locks protect only JSON transactions. Per-job locks protect a single spawn;
downloads run separately and never determine whether a dependency is ready.
"""
from pipeline.s04_collect_outputs import collect_outputs
from pathlib import Path
import time

from orchestration import model_capabilities, state
from orchestration.sdk_session import SDKSession
from orchestration.input_store import MANAGED_PREFIX, stage_input
from runtime.tasks.task_identity import intent_digest
from runtime.tasks.task_spec import prepare, result_prefix, execution_signature
from runtime.tasks.job_client import remote_cache_fingerprint, remote_record, check_record, recovery_path
from runtime.tasks.result_integrity import verify_remote_media
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
        from runtime.delivery.huggingface import ArchiveBatcher
        self.archive_batcher = ArchiveBatcher(self.modal)

    def fingerprint(self, spec):
        key = intent_digest(spec)
        with self.session.keyed_lock(("fingerprint", key)):
            if key not in self.fingerprints:
                self.fingerprints[key] = remote_cache_fingerprint(spec, self.volume)
            return self.fingerprints[key]

    def upload(self, directory, artifact, *, managed=False):
        return stage_input(self.volume, self.session, directory, artifact, managed=managed)

    def submit(self, spec, item, prefix, fingerprint, *, deployment_version=None):
        for field in ("first_frame_artifact", "last_frame_artifact"):
            ref = item.get(field)
            if ref and ref["path"].startswith(MANAGED_PREFIX) and not getattr(self, "managed_workflow", False):
                raise ValueError("Managed input requires a durable workflow controller")
        key = intent_digest(spec)
        handle_key = (key, deployment_version)
        execution_hash = execution_signature(prepare(spec, item)[1], fingerprint)
        # A stable class tag can still receive an old rolling container.
        # Guard the invocation itself, without paid version-pinned lookup.
        guard = {"expected_deployment_version": deployment_version} if deployment_version is not None else {}
        def spawn():
            with self.session.keyed_lock(("model", handle_key)):
                if handle_key not in self.models:
                    self.models[handle_key] = self.modal.Cls.from_name(
                        spec.get("app_name", "modal-model-" + spec["service"]),
                        spec.get("modal_class", "Model"))()
            return self.models[handle_key].generate_one.spawn(item, prefix, fingerprint, key, **guard).object_id

        return self.submission_ledger().submit(prefix, item["id"], execution_hash, spawn)

    def preflight_result(self, record):
        """Existing evidence forbids a fresh GPU submission, even after local state loss."""
        result = remote_record(self.volume, record["prefix"], record["id"])
        if result is not None:
            check_record(record, result)
            if result.get("status") == "completed" and not verify_remote_media(self.volume, record, result):
                raise ValueError("Remote result damaged; refusing GPU submission")
        return result

    def submission_ledger(self):
        from orchestration.protocol import SUBMISSION_REGISTRY
        from orchestration.submissions import SubmissionLedger
        with self.session.lock:
            if not hasattr(self, "_submission_ledger"):
                self._submission_ledger = SubmissionLedger(self.modal.Dict.from_name(SUBMISSION_REGISTRY, create_if_missing=True))
            return self._submission_ledger

    def call_handle(self, call_id):
        return self.session.handle(("call", call_id), lambda: self.modal.FunctionCall.from_id(call_id))

    def _committed_result(self, record, *, recover=False):
        result = remote_record(self.volume, record["prefix"], record["id"])
        if result:
            check_record(record, result)
            if recover and result.get("status") == "collecting" and record.get("call_id"):
                from runtime.tasks.task_lifecycle import terminal_call_status
                from runtime.utils.volume_records import put_once
                call_id = record["call_id"]
                terminal = terminal_call_status(self.modal, call_id)
                if terminal and verify_remote_media(self.volume, record, result):
                    # Publish one immutable recovery receipt instead of overwriting GPU metadata.
                    marker = {"source_hash": intent_digest(result), "call_id": call_id,
                              "terminal_status": terminal}
                    if put_once(self.volume, recovery_path(record["prefix"], record["id"]), marker):
                        # Successful immutable publication plus the already verified
                        # terminal receipt is sufficient. Only a lost race needs a read.
                        result = {**result, "status": "completed"}
                    else:
                        result = remote_record(self.volume, record["prefix"], record["id"])
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
        persisted_call = record.get("call_id")
        submission = (record["prefix"], record["id"], record["execution_hash"])
        # A positive receipt from SubmissionLedger is immutable under the
        # reservation protocol. Never cache absence or a mutable job status.
        call_id = persisted_call or self.session.accepted_call(submission)
        identity = (*submission, persisted_call)
        if not persisted_call or record.get("status") == "uncertain" or self.session.ledger_due(identity, _LEDGER_INTERVAL):
            committed = self._committed_result({**record, "call_id": call_id}, recover=True)
            if committed is not None:
                self.session.forget_ledger(identity)
                self.session.forget_accepted_call(submission)
                if call_id:
                    self.session.forget_handle(("call", call_id))
                return committed
            self.session.note_ledger_read(identity)
        if not call_id and record.get("prefix") and record.get("id"):
            entry = self.submission_ledger().lookup(record["prefix"], record["id"], record["execution_hash"])
            if entry and entry.get("call_id"):
                call_id = entry["call_id"]
                if not isinstance(call_id, str):
                    raise ValueError("Invalid accepted submission call ID")
                self.session.remember_accepted_call(submission, call_id)
        if not call_id:
            return None
        if not persisted_call:
            record = {**record, "call_id": call_id}
        from modal.exception import TimeoutError as ModalTimeoutError
        try:
            result = self.call_handle(call_id).get(timeout=0)
        except (TimeoutError, ModalTimeoutError) as exc:
            if type(exc) in (TimeoutError, ModalTimeoutError):
                return None
            self.session.forget_handle(("call", call_id))
            self.session.forget_ledger(identity)
            committed = self._committed_result(record, recover=True)
            if committed is not None:
                self.session.forget_accepted_call(submission)
                return committed
            raise
        except Exception:
            self.session.forget_handle(("call", call_id))
            self.session.forget_ledger(identity)
            committed = self._committed_result(record, recover=True)
            if committed is not None:
                self.session.forget_accepted_call(submission)
                return committed
            raise
        self.session.forget_handle(("call", call_id))
        self.session.forget_ledger(identity)
        committed = self._committed_result(record, recover=True)
        if committed is not None:
            self.session.forget_accepted_call(submission)
            return committed
        check_record(record, result)
        if result.get("status") != "completed":
            raise ValueError("Cloud call returned an incomplete result")
        self.session.forget_accepted_call(submission)
        return result

    def download(self, directory, job, record, plan):
        recorder = local_recorder(state.telemetry_directory(directory), task_id=f"{plan['project']}/{plan['scene']}",
                                  job_key=job["key"], call_id=record.get("call_id"))
        with recorder.span("download") as fields:
            return self._download(directory, job, record, plan, fields)

    def _download(self, directory, job, record, plan, fields):
        from runtime.delivery.local import download_to_local
        return download_to_local(self.volume, directory, job, record, fields)

    def delivery_store(self, plan):
        if plan.get("delivery_protocol") != 3:
            return self.volume
        from runtime.delivery import v3
        return self.session.handle(("delivery-v3-ledger", plan["delivery_volume"]),
                                   lambda: v3.ledger(self.modal, plan))

    def register_delivery(self, job, record, plan):
        from runtime.delivery import outbox
        intent = outbox.make_intent(job, record, plan)
        if plan.get("delivery_protocol") == 3:
            from runtime.delivery import v3
            return v3.register(self.delivery_store(plan), intent)
        return outbox.register(self.volume, intent)

    def note_delivery_call(self, intent, call_id, *, plan=None):
        from runtime.delivery import outbox
        store = self.delivery_store(plan or {})
        outbox.immutable(store, outbox.path(intent["id"], "generation-call.json"),
                         {"call_id": call_id})

    def archive(self, directory, job, record, plan):
        if plan.get("delivery_protocol") in (2, 3):
            from runtime.delivery import outbox
            durable_v3 = plan["delivery_protocol"] == 3
            store = self.delivery_store(plan)
            if durable_v3:
                from runtime.delivery import v3
            intent = outbox.make_intent(job, record, plan)
            # The completed scheduler record already contains the verified
            # generation reference. Do not re-read the generation ledger on
            # every PC2 poll; bind the receipt to that exact artifact instead.
            if record.get("delivery_id") not in (None, intent["id"]):
                raise ValueError("Delivery identity changed")
            if record.get("delivery_id") is None and outbox.read(
                    store, outbox.path(intent["id"], "intent.json")) is None:
                if durable_v3:
                    v3.register(store, intent)
                else:
                    outbox.register(store, intent)
            from runtime.delivery.huggingface import archive_request
            request = archive_request(job, record, plan)
            row = outbox.receipt(store, intent, request)
            if row is not None:
                try:
                    return outbox.derivative_receipt(store, intent, row)
                except (ValueError, KeyError, TypeError):
                    return {**row, "derivative_status": "failed"}
                except OSError:
                    return {**row, "derivative_status": "pending"}
            blocked = outbox.read(store, outbox.path(intent["id"], "blocked.json"))
            if blocked:
                raise RuntimeError("Durable archive blocked: " + blocked["kind"])
            if durable_v3:
                # The producer event and scheduled scan are authoritative;
                # local observation can send a rate-limited repair hint.
                with self.session.keyed_lock("delivery-v3-wakeup"):
                    now = time.monotonic()
                    wake = getattr(self, "_v3_wake", {})
                    should_notify = now >= wake.get(intent["id"], 0)
                    if should_notify:
                        wake[intent["id"]] = now + 60
                        self._v3_wake = {k:v for k,v in wake.items() if v > now}
                if should_notify:
                    v3.notify(intent["id"], plan["delivery_app"])
                return None
            with self.session.keyed_lock("delivery-v2-wakeup"):
                now = time.monotonic()
                if now >= getattr(self, "_delivery_wakeup", 0):
                    self._delivery_wakeup = now + 15
                    pending = getattr(self, "_delivery_scan_call", None)
                    if pending is not None:
                        try:
                            pending.get(timeout=0)
                        except (TimeoutError, self.modal.exception.TimeoutError):
                            return None
                        except Exception:
                            # Do not assume an ambiguous RPC failure means the
                            # scan ended; the scheduled scanner is the fallback.
                            from runtime.tasks.task_lifecycle import terminal_call_status
                            if not terminal_call_status(self.modal, pending.object_id):
                                return None
                    self._delivery_scan_call = self.modal.Function.from_name(outbox.app_name(), "scan").spawn()
            return None
        from runtime.delivery.huggingface import archive_request
        return self.archive_batcher.archive(archive_request(job, record, plan))


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
        if ref["path"].startswith(MANAGED_PREFIX) and not getattr(backend, "managed_workflow", False):
            raise ValueError("Managed input requires a durable workflow controller")
        item[field + "_artifact"] = ref
    return item
