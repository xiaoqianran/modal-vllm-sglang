"""Durable per-shot plans and append-only controller ownership.

Volume V2 no-overwrite publication is the admission barrier. A controller
successor can be admitted only after the exact previous Modal input is
terminal. GPU submission reservations remain untouched.
"""
import io
import json
import re

from orchestration import model_capabilities
from orchestration.input_store import LIFECYCLE, MANAGED_PREFIX
from orchestration.protocol import VERSION, run_id, state_path
from runtime.config.storage import VOLUME_SUB_PATH, volume_key
from runtime.tasks.artifacts import image_reference
from runtime.tasks.job_client import check_record
from runtime.tasks.task_identity import intent_digest, safe_id
from runtime.tasks.task_spec import result_prefix, execution_signature

SCHEMA = 1
MAX_RECORD_BYTES = 16 * 1024 * 1024
MAX_OWNERS = 128
TERMINAL = {"completed", "failed"}


class ControllerBusy(RuntimeError):
    pass


def plan_path(identity):
    return state_path(identity).rsplit("/", 1)[0] + "/plan.json"


def retirement_path(identity):
    return state_path(identity).rsplit("/", 1)[0] + "/retired.json"


def read_retirement(volume, manifest):
    return _read(volume, retirement_path(manifest["identity"]))


def owner_path(identity, parent=None):
    return state_path(identity).rsplit("/", 1)[0] + "/owners/" + (
        intent_digest(parent) if parent else "initial") + ".json"


def _read(volume, path):
    data = bytearray()
    try:
        for chunk in volume.read_file(volume_key("/" + path)):
            data.extend(chunk)
            if len(data) > MAX_RECORD_BYTES:
                raise ValueError("Cloud workflow record too large")
    except FileNotFoundError:
        return None
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("Invalid cloud workflow record")
    return value


def _put_once(volume, path, value):
    key = volume_key("/" + path)
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(data) > MAX_RECORD_BYTES:
        raise ValueError("Cloud workflow record too large")
    try:
        with volume.batch_upload(force=False) as batch:
            batch.put_file(io.BytesIO(data), key)
        return True
    except Exception as exc:
        from modal.exception import InvalidError
        if not (isinstance(exc, FileExistsError) or
                isinstance(exc, InvalidError) and str(exc) == f'path "{key}" already exists'):
            raise
        return False


def validate_payload(payload, identity, volume_name):
    if (not isinstance(payload, dict) or payload.get("recovery_schema") != SCHEMA
            or payload.get("version") != VERSION):
        raise ValueError("Unsupported durable workflow plan")
    if payload.get("input_lifecycle") not in (None, LIFECYCLE):
        raise ValueError("Unsupported input lifecycle")
    plan = payload["plan"]
    if (plan.get("transport") != "sdk" or plan.get("volume") != volume_name
            or plan.get("volume_sub_path") != VOLUME_SUB_PATH
            or plan.get("adapter_version") != model_capabilities.VERSION
            or plan.get("imports") != {}
            or plan.get("hash") != intent_digest({k: v for k, v in plan.items() if k != "hash"})):
        raise ValueError("Durable plan/storage identity mismatch")
    for field in ("project", "scene", "name"):
        safe_id(plan[field])
    jobs = plan["jobs"]
    items = {job["item"] for job in jobs}
    if len(items) != 1 or run_id(plan, next(iter(items))) != identity:
        raise ValueError("Cloud workflow identity mismatch")
    prior = {}
    for job in jobs:
        step, item = safe_id(job["step"]), safe_id(job["item"])
        if job["key"] != f"{step}/{item}" or job["key"] in prior:
            raise ValueError("Invalid workflow job identity")
        cap = model_capabilities.CAPABILITIES[job["uses"]]
        spec = plan["services"][cap["service"]]["spec"]
        if spec["service"] != cap["service"]:
            raise ValueError("Workflow model binding mismatch")
        for field, binding in job["inputs"].items():
            if field not in ("first_frame", "last_frame"):
                continue
            if not isinstance(binding, dict):
                raise ValueError("Untracked workflow input")
            if "job" in binding:
                if (binding["job"] not in prior or binding["output"] != "image"
                        or prior[binding["job"]] != "image"):
                    raise ValueError("Invalid upstream workflow input")
            else:
                if binding.get("storage") != "volume" or binding.get("type") != "image":
                    raise ValueError("Workflow input has not been persisted")
                ref = image_reference({k: binding[k] for k in ("path", "sha256", "size")})
                if ref["path"].startswith(MANAGED_PREFIX) and (
                        payload.get("input_lifecycle") != LIFECYCLE
                        or ref["path"] != MANAGED_PREFIX + ref["sha256"]):
                    raise ValueError("Invalid managed workflow input reference")
        prior[job["key"]] = cap["output"]
    initial = payload["initial_jobs"]
    if set(initial) != set(prior):
        raise ValueError("Initial job identities mismatch")
    if "provisioning" in payload:
        pins = payload["provisioning"]
        if set(pins) != set(plan["services"]) or any(not isinstance(v, str) or not v for v in pins.values()):
            raise ValueError("Invalid pinned model preparation")
    validate_jobs(plan, initial)
    return plan


def validate_jobs(plan, records):
    if not isinstance(records, dict) or set(records) != {j["key"] for j in plan["jobs"]}:
        raise ValueError("Unexpected cloud job identities")
    for job in plan["jobs"]:
        record = records[job["key"]]
        if not isinstance(record, dict) or record.get("status") not in {
                "pending", "blocked", "submitting", "queued", "uncertain", "completed", "failed"}:
            raise ValueError("Invalid persisted job state")
        cap = model_capabilities.CAPABILITIES[job["uses"]]
        spec = plan["services"][cap["service"]]["spec"]
        if record.get("prefix") is not None or record["status"] in {"submitting", "queued", "uncertain", "completed"}:
            expected = result_prefix(spec, plan["project"], plan["scene"], job["uses"].rsplit(".", 1)[1])
            if record.get("prefix") != expected or record.get("id") != "j" + intent_digest(job["key"])[:32]:
                raise ValueError("Persisted GPU job identity mismatch")
            signature, fingerprint = record.get("intent_hash"), record.get("cache_fingerprint")
            if (not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{64}", signature)
                    or not isinstance(fingerprint, str) or not fingerprint
                    or record.get("execution_hash") != execution_signature(signature, fingerprint)):
                raise ValueError("Persisted GPU execution signature mismatch")
            if record["status"] == "queued" and not record.get("call_id"):
                raise ValueError("Queued GPU job missing call receipt")
        if record["status"] == "completed":
            result = record["result"]
            check_record(record, result)
            if result.get("status") != "completed":
                raise ValueError("Invalid completed job evidence")
            expected = {"type": cap["output"], "storage": "volume",
                        "path": record["prefix"] + "/" + result["file"],
                        "sha256": result["sha256"], "size": result["bytes"]}
            if (record.get("artifacts") != {cap["output"]: expected}
                    or type(result["bytes"]) is not int or result["bytes"] <= 0
                    or not re.fullmatch(r"[0-9a-f]{64}", result["sha256"])):
                raise ValueError("Invalid persisted artifact reference")


def publish_plan(volume, payload, identity, volume_name):
    validate_payload(payload, identity, volume_name)
    path = plan_path(identity)
    existing = _read(volume, path)
    record = {"schema": SCHEMA, "identity": identity, "volume": volume_name,
              "volume_id": volume.object_id, "payload_hash": intent_digest(payload), "payload": payload}
    if existing is None and not _put_once(volume, path, record):
        existing = _read(volume, path)
        if existing is None:
            raise RuntimeError("Durable workflow plan publication not visible")
    if existing is not None and existing != record:
        raise ValueError("Cloud workflow plan already exists with different content")
    return record


def load_plan(volume, identity, volume_name, expected_hash=None):
    record = _read(volume, plan_path(identity))
    if record is None:
        raise FileNotFoundError("No durable workflow plan; legacy runs cannot be reconstructed")
    payload = record.get("payload")
    if (record.get("schema") != SCHEMA or record.get("identity") != identity
            or record.get("volume") != volume_name or record.get("volume_id") != volume.object_id
            or record.get("payload_hash") != intent_digest(payload)
            or expected_hash is not None and record["payload_hash"] != expected_hash):
        raise ValueError("Durable workflow payload identity mismatch")
    validate_payload(payload, identity, volume_name)
    return record


def read_state(volume, manifest):
    record = _read(volume, state_path(manifest["identity"]))
    if record is None:
        return None
    if (record.get("version") != VERSION or record.get("payload_hash") != manifest["payload_hash"]
            or record.get("status") not in {"running", "uncertain", "completed", "failed"}):
        raise ValueError("Durable workflow checkpoint identity mismatch")
    validate_jobs(manifest["payload"]["plan"], record["jobs"])
    if record["status"] in TERMINAL:
        statuses = {j["status"] for j in record["jobs"].values()}
        if (not statuses <= TERMINAL or
                (record["status"] == "completed") != (statuses == {"completed"})):
            raise ValueError("Inconsistent terminal workflow checkpoint")
    return record


def latest_owner(volume, manifest):
    parent = None
    for _ in range(MAX_OWNERS):
        owner = _read(volume, owner_path(manifest["identity"], parent))
        if owner is None:
            return parent
        if (owner.get("schema") != SCHEMA or owner.get("identity") != manifest["identity"]
                or owner.get("payload_hash") != manifest["payload_hash"]
                or owner.get("parent") != (intent_digest(parent) if parent else None)
                or not isinstance(owner.get("call_id"), str) or not owner["call_id"].startswith("fc-")
                or not isinstance(owner.get("input_id"), str) or not owner["input_id"]):
            raise ValueError("Invalid workflow controller ownership chain")
        parent = owner
    raise RuntimeError("Controller recovery history exceeds supported bound")


def _base_input_id(value):
    # SDK 1.6.4 current_input_id() may include an execution suffix
    # (in-<id>:<timestamp>-<attempt>); the call graph reports in-<id>.
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(in-[A-Za-z0-9-]+)(?::[0-9]+-[0-9]+)?", value)
    return match.group(1) if match else None


def _terminal_status(modal_api, owner):
    """Only the exact platform input's terminal status proves death."""
    from modal.types import InputStatus
    input_id = _base_input_id(owner["input_id"])
    if input_id is None:
        return None
    try:
        graph = modal_api.FunctionCall.from_id(owner["call_id"]).get_call_graph()
    except Exception:
        return None
    matches = [node for node in graph if node.function_call_id == owner["call_id"]
               and node.input_id == input_id]
    terminal = {InputStatus.SUCCESS, InputStatus.FAILURE, InputStatus.INIT_FAILURE,
                InputStatus.TERMINATED, InputStatus.TIMEOUT}
    return matches[0].status.name if len(matches) == 1 and matches[0].status in terminal else None


def owner_terminal(modal_api, owner):
    return _terminal_status(modal_api, owner) is not None


def terminal_path(identity, owner):
    return state_path(identity).rsplit("/", 1)[0] + "/terminal/" + intent_digest(owner) + ".json"


def terminal_evidence(volume, manifest, owner):
    value = _read(volume, terminal_path(manifest["identity"], owner))
    if value is not None:
        expected = {"schema": SCHEMA, "owner_hash": intent_digest(owner),
                    "call_id": owner["call_id"], "input_id": _base_input_id(owner["input_id"])}
        if (any(value.get(k) != v for k, v in expected.items()) or
                value.get("status") not in {"SUCCESS", "FAILURE", "INIT_FAILURE", "TERMINATED", "TIMEOUT"}):
            raise ValueError("Invalid durable controller terminal evidence")
    return value


def seal_terminal(volume, manifest, modal_api, owner):
    """Persist verified terminal evidence before a rollout can remove call history."""
    evidence = terminal_evidence(volume, manifest, owner)
    if evidence is not None:
        return evidence
    status = _terminal_status(modal_api, owner)
    if status is None:
        return None
    value = {"schema": SCHEMA, "owner_hash": intent_digest(owner),
             "call_id": owner["call_id"], "input_id": _base_input_id(owner["input_id"]),
             "status": status}
    if _put_once(volume, terminal_path(manifest["identity"], owner), value):
        return value
    evidence = terminal_evidence(volume, manifest, owner)
    if evidence is None:
        raise RuntimeError("Controller terminal evidence publication not visible")
    return evidence


def acquire_owner(volume, manifest, modal_api, *, recover=False, claim_registry=None):
    identity = manifest["identity"]
    # Preserve the original global claim barrier for legacy callers. Ownership
    # and its successors live in the durable Volume, never in expiring Dict keys.
    if claim_registry is not None:
        guard = {"schema": SCHEMA, "payload_hash": manifest["payload_hash"]}
        if not claim_registry.put(identity, guard, skip_if_exists=True):
            if claim_registry.get(identity) != guard:
                raise ControllerBusy("Legacy or different workflow claim; refusing takeover")
    prior = latest_owner(volume, manifest)
    if prior is not None and (not recover or not seal_terminal(volume, manifest, modal_api, prior)):
        raise ControllerBusy("Workflow controller already claimed or terminal proof unavailable")
    if prior is None and read_state(volume, manifest) is not None:
        raise ControllerBusy("Checkpoint exists without durable owner; refusing legacy takeover")
    call_id, input_id = modal_api.current_function_call_id(), modal_api.current_input_id()
    if not isinstance(call_id, str) or not call_id.startswith("fc-") or not input_id:
        raise RuntimeError("Durable controller must run as a Modal function input")
    owner = {"schema": SCHEMA, "identity": identity, "payload_hash": manifest["payload_hash"],
             "parent": intent_digest(prior) if prior else None,
             "call_id": call_id, "input_id": input_id}
    if not _put_once(volume, owner_path(identity, prior), owner):
        raise ControllerBusy("Another controller won the durable ownership claim")
    return owner


def execute(volume, manifest, modal_api, runner, *, recover=False, claim_registry=None):
    previous = read_state(volume, manifest)
    if previous is not None and previous["status"] in TERMINAL:
        return previous
    if read_retirement(volume, manifest) is not None:
        raise ControllerBusy("Retired workflow cannot resume execution")
    acquire_owner(volume, manifest, modal_api, recover=recover, claim_registry=claim_registry)
    # Never use a mount's cached state from before the previous owner exited.
    previous = read_state(volume, manifest)
    if previous is not None and previous["status"] in TERMINAL:
        return previous
    return runner(manifest["payload"], manifest["identity"], durable=True, previous=previous)
