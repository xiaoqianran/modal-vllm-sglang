"""Validate durable result records against their content and task identity."""
import hashlib
import re
from pathlib import Path

from runtime.config.storage import volume_key
from runtime.utils.outputs import validate_path_component

from runtime.utils.files import read_json, sha256_file


MEDIA_SUFFIXES = (".png", ".jpg", ".webp", ".mp4")


def validate_media_record(job, record):
    shot = validate_path_component(job["id"])
    if (record.get("id") != shot
            or record.get("file") not in {shot + suffix for suffix in MEDIA_SUFFIXES}
            or type(record.get("bytes")) is not int or record["bytes"] <= 0
            or not isinstance(record.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])):
        raise ValueError("Invalid media result receipt")


def verify_chunks(chunks, record):
    digest = hashlib.sha256()
    size = 0
    for chunk in chunks:
        size += len(chunk)
        if size > record["bytes"]:
            return False
        digest.update(chunk)
    return size == record["bytes"] and digest.hexdigest() == record["sha256"]


def verify_remote_media(volume, job, record):
    validate_media_record(job, record)
    for part in job["prefix"].split("/"):
        validate_path_component(part)
    try:
        return verify_chunks(volume.read_file(volume_key(
            f"/{job['prefix']}/{record['file']}")), record)
    except FileNotFoundError:
        return False


def verify_local_media(path, record):
    try:
        with Path(path).open("rb") as stream:
            return verify_chunks(iter(lambda: stream.read(1024 * 1024), b""), record)
    except FileNotFoundError:
        return False


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
