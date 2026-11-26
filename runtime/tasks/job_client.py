"""Read and validate durable model results and cache fingerprints."""
import json
from runtime.tasks.task_identity import intent_digest
from runtime.config.model_cache import cache_directory, component_specs
from runtime.config.storage import volume_key
from runtime.tasks.result_integrity import validate_media_record
from runtime.tasks.task_lifecycle import CALL_TERMINAL


def remote_cache_fingerprint(spec, volume):
    try:
        cache = json.loads(b"".join(volume.read_file(
            volume_key(cache_directory(spec).removeprefix("/data") + "/model-ready.json"))))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Remote cache manifest missing for {spec['service']}; "
                           "run download_model.py --migrate-only or prepare it on CPU first") from exc
    if cache.get("version") != 1 or cache.get("cache_spec") != intent_digest(component_specs(spec)):
        raise RuntimeError("Remote cache differs from current model configuration; prepare or migrate it on CPU")
    return intent_digest(cache)


def recovery_path(prefix, shot):
    return f"{prefix}/{shot}.recovered.json"


def remote_record(volume, prefix, shot):
    try:
        record = json.loads(b"".join(volume.read_file(volume_key(f"/{prefix}/{shot}.json"))))
    except FileNotFoundError:
        return None
    if record.get("status") == "collecting":
        try:
            marker = json.loads(b"".join(volume.read_file(volume_key("/" + recovery_path(prefix, shot)))))
        except FileNotFoundError:
            return record
        if (marker.get("source_hash") != intent_digest(record)
                or not isinstance(marker.get("call_id"), str)
                or not marker["call_id"].startswith("fc-")
                or marker.get("terminal_status") not in CALL_TERMINAL):
            raise ValueError("Invalid recovered result evidence")
        validate_media_record({"id": shot}, record)
        return {**record, "status": "completed"}
    return record


def check_record(job, record):
    if (record.get("intent_hash") != job["intent_hash"]
            or record.get("execution_hash") != job["execution_hash"]):
        raise ValueError("Task/configuration/cache conflict; refusing to overwrite or resubmit")
    if record.get("status") in {"collecting", "completed"}:
        validate_media_record(job, record)
