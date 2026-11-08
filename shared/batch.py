"""Small model-agnostic helpers for resumable inference batches."""
import hashlib
import json
import re
from pathlib import Path

from shared.files import read_json, sha256_file

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def safe_id(value):
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        raise ValueError(f"Invalid name or id: {value!r}")
    return value


def intent_digest(data):
    payload = json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def completed_output(output_path, metadata_path, signature, *, validate=None):
    """Skip only an intact result with matching signature and content hash."""
    output_path, metadata_path = Path(output_path), Path(metadata_path)
    if not output_path.is_file() or not metadata_path.is_file():
        return False
    try:
        if validate is not None and not validate(output_path):
            return False
        metadata = read_json(metadata_path)
        return (metadata.get("status") == "completed"
                and metadata.get("intent_hash") == signature
                and metadata.get("sha256") == sha256_file(output_path))
    except (OSError, ValueError, TypeError, KeyError):
        return False


def counts(results, total, *, completed="completed", skipped="skipped", failed="failed"):
    return {
        "completed": sum(entry.get("status") == completed for entry in results),
        "skipped": sum(entry.get("status") == skipped for entry in results),
        "failed": sum(entry.get("status") == failed for entry in results),
        "unattempted": total - len(results),
    }
