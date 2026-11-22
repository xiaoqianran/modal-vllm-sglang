"""Read and validate durable model results and cache fingerprints."""
import json
from runtime.tasks.task_identity import intent_digest
from runtime.config.model_cache import cache_directory, component_specs


def remote_cache_fingerprint(spec, volume):
    try:
        cache = json.loads(b"".join(volume.read_file(
            cache_directory(spec).removeprefix("/mnt/modal") + "/model-ready.json")))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Remote cache manifest missing for {spec['service']}; "
                           "run download_model.py --migrate-only or prepare it on CPU first") from exc
    if cache.get("version") != 1 or cache.get("cache_spec") != intent_digest(component_specs(spec)):
        raise RuntimeError("Remote cache differs from current model configuration; prepare or migrate it on CPU")
    return intent_digest(cache)


def remote_record(volume, prefix, shot):
    try:
        return json.loads(b"".join(volume.read_file(f"/{prefix}/{shot}.json")))
    except FileNotFoundError:
        return None


def check_record(job, record):
    if (record.get("intent_hash") != job["intent_hash"]
            or record.get("execution_hash") != job["execution_hash"]):
        raise ValueError("Task/configuration/cache conflict; refusing to overwrite or resubmit")
    if record.get("status") == "completed":
        if (record.get("id") != job["id"]
                or record.get("file") not in {job["id"] + s for s in (".png", ".jpg", ".webp", ".mp4")}
                or not isinstance(record.get("sha256"), str) or len(record["sha256"]) != 64):
            raise ValueError("Invalid completed result")
