"""Atomic results and streaming transfers for both model pipelines."""
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def atomic_write(path, data):
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


def atomic_json(path, obj):
    atomic_write(path, (json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8"))


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_volume_file(volume, remote_path, destination, *, expected_sha256=None, kind=None, refuse_existing=False):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(destination.suffix + ".part")
    try:
        with temp.open("wb") as stream:
            for chunk in volume.read_file(remote_path):
                stream.write(chunk)
        if expected_sha256 is not None and sha256_file(temp) != expected_sha256:
            raise ValueError("Downloaded content checksum mismatch")
        if kind is not None:
            from shared.model_service import media_suffix
            with temp.open("rb") as stream:
                media_suffix(stream.read(32), kind)
        if refuse_existing and destination.exists():
            raise FileExistsError(destination)
        os.replace(temp, destination)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


@contextmanager
def file_lock(directory, *, filename=".runner.lock"):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / filename).open("a+b") as stream:
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
    if not isinstance(relative, (str, Path)):
        raise ValueError("Expected a file path")
    path = (root / relative).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError(f"Path must stay inside {root}: {relative}")
    return path
