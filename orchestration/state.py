"""Single-writer state and verified, atomically published artifacts."""
from pathlib import Path

from shared.files import sha256_file


from shared.files import atomic_json as save, atomic_write as write, file_lock as lock


from shared.files import local_path

from shared.media import image_format


def publish(root, folder, name, data, *, filename=None, refuse_existing=False):
    if name == "image":
        suffix, mime = image_format(data)
    elif name == "video" and len(data) >= 32 and data[4:8] == b"ftyp":
        suffix, mime = ".mp4", "video/mp4"
    else:
        raise ValueError("Invalid output format")
    if filename is not None:
        # Public outputs are flat; per-step manifests live in a separate folder.
        if (not isinstance(filename, str) or not filename.isascii()
                or not filename.replace("-", "").replace("_", "").isalnum()):
            raise ValueError("Invalid output filename")
    relative = f"{folder}/{filename or name}{suffix}" if folder else f"{filename or name}{suffix}"
    path = local_path(root, relative)
    if refuse_existing and path.exists():
        raise FileExistsError(f"Output already exists, refusing overwrite: {path}")
    write(path, data)
    return {"type": name, "path": path.relative_to(Path(root).resolve()).as_posix(),
            "size": len(data), "sha256": sha256_file(path), "mime": mime}


def valid(root, artifact):
    try:
        path = local_path(root, artifact["path"])
        return (path.is_file() and path.stat().st_size == artifact["size"]
                and sha256_file(path) == artifact["sha256"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
