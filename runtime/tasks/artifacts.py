"""Content-verified cloud image references; no host paths cross this boundary."""
import base64
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
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


def _verified_frame(chunks, ref):
    data = bytearray()
    digest = hashlib.sha256()
    for chunk in chunks:
        if len(data) + len(chunk) > ref["size"]:
            raise ValueError("Cloud image size mismatch")
        digest.update(chunk)
        data.extend(chunk)
    if len(data) != ref["size"] or digest.hexdigest() != ref["sha256"]:
        raise ValueError("Cloud image content mismatch")
    image_format(data)
    return base64.b64encode(data).decode("ascii")


def _read_frame(ref, volume, mount_root):
    # A snapshot/container mount can lag behind the committed SDK view.
    # Reuse only exact, content-verified mounted bytes; otherwise use the
    # existing authoritative SDK read. Never reload open model mmap files.
    if mount_root is not None:
        root = Path(mount_root).resolve()
        try:
            local = (root / ref["path"]).resolve()
            if local.is_relative_to(root) and local.is_file():
                with local.open("rb") as stream:
                    return _verified_frame(iter(lambda: stream.read(1024 * 1024), b""), ref)
        except (OSError, ValueError):
            pass
    return _verified_frame(volume.read_file(volume_key("/" + ref["path"])), ref)


def resolve_frames(payload, volume, *, mount_root=None):
    """Resolve at most two independent frames with strict size/hash validation."""
    payload = dict(payload)
    refs = {}
    for field in ("first_frame", "last_frame"):
        ref = payload.pop(field + "_artifact", None)
        if ref is not None:
            refs[field] = image_reference(ref)
    if len(refs) == 1:
        field, ref = next(iter(refs.items()))
        payload[field + "_base64"] = _read_frame(ref, volume, mount_root)
    elif refs:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {field: pool.submit(_read_frame, ref, volume, mount_root)
                       for field, ref in refs.items()}
            for field, future in futures.items():
                payload[field + "_base64"] = future.result()
    return payload
