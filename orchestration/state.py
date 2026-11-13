"""Single-writer state and verified, atomically published artifacts."""
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from shared.files import sha256_file


def save(path, value):
    write(path, (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode())


def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def lock(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".runner.lock").open("a+b") as stream:
        if stream.seek(0, 2) == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(f"Another runner holds {directory}") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def local_path(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError(f"Path must stay inside {root}: {relative}")
    return path


def image_format(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    raise ValueError("Expected PNG, JPEG or WebP content")


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
