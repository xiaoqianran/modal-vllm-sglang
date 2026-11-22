"""Content-verified cloud image references; no host paths cross this boundary."""
import base64
import hashlib
from pathlib import PurePosixPath
import re

from runtime.utils.media import MAX_FRAME, image_format
from runtime.config.storage import volume_key


def image_reference(value):
    if not isinstance(value, dict) or set(value) != {"path", "sha256", "size"}:
        raise ValueError("Cloud image needs path, sha256 and size")
    path = value["path"]
    if (not isinstance(path, str) or not path or "\\" in path
            or PurePosixPath(path).is_absolute() or ".." in path.split("/")
            or PurePosixPath(path).as_posix() != path
            or path.split("/")[0] not in {"model-results", "workflow-inputs"}):
        raise ValueError("Invalid cloud image path")
    if not isinstance(value["sha256"], str) or not re.fullmatch("[0-9a-f]{64}", value["sha256"]):
        raise ValueError("Invalid cloud image SHA256")
    if type(value["size"]) is not int or not 0 < value["size"] <= MAX_FRAME:
        raise ValueError("Cloud image must be 1 byte to 20 MiB")
    return dict(value)


def resolve_frames(payload, volume):
    """Read committed storage through the SDK, without reloading model mmap mounts."""
    payload = dict(payload)
    for field in ("first_frame", "last_frame"):
        ref = payload.pop(field + "_artifact", None)
        if ref is None:
            continue
        ref = image_reference(ref)
        chunks, size = [], 0
        for chunk in volume.read_file(volume_key("/" + ref["path"])):
            size += len(chunk)
            if size > ref["size"]:
                raise ValueError("Cloud image size mismatch")
            chunks.append(chunk)
        data = b"".join(chunks)
        if size != ref["size"] or hashlib.sha256(data).hexdigest() != ref["sha256"]:
            raise ValueError("Cloud image content mismatch")
        image_format(data)
        payload[field + "_base64"] = base64.b64encode(data).decode("ascii")
    return payload
