"""Validate durable result records against their content and task identity."""
from pathlib import Path

from runtime.utils.files import read_json, sha256_file


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
