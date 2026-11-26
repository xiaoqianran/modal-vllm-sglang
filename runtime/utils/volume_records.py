"""Immutable JSON publication on Volume V2."""
import io
import json

from runtime.config.storage import volume_key


def put_once(volume, path, value, *, max_bytes=16 * 1024 * 1024):
    key = volume_key("/" + path)
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(data) > max_bytes:
        raise ValueError("Volume record too large")
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
