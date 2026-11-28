"""Atomic results and streaming transfers for both model pipelines."""
import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def replace_with_retry(source, destination):
    """Bounded retry for transient Windows sharing violations; never truncate."""
    delays = (0.02, 0.05, 0.1, 0.2, 0.4, 0.8)
    for delay in (*delays, None):
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            if delay is None or getattr(exc, "winerror", None) not in {5, 32, 33}:
                raise
            time.sleep(delay)


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        replace_with_retry(temporary, path)
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


def download_volume_file(volume, remote_path, destination, *, expected_sha256=None, kind=None, refuse_existing=False, max_bytes=None):
    # Modal Volume V2 supports independently verified blocks. Keep the
    # public streaming path for small files, test doubles and other backends.
    if expected_sha256 is not None and max_bytes is not None and max_bytes >= 8 * 1024 * 1024:
        from runtime.utils.volume_download import resumable_download
        result = resumable_download(volume, remote_path, destination,
                                    expected_sha256=expected_sha256, kind=kind,
                                    refuse_existing=refuse_existing, max_bytes=max_bytes)
        if result is not None:
            return result
    started = time.perf_counter()
    first_chunk_seconds = None
    size = 0
    digest = hashlib.sha256() if expected_sha256 is not None else None
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=destination.name + ".", suffix=".part", dir=destination.parent)
    temp = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            for chunk in volume.read_file(remote_path):
                if max_bytes is not None and size + len(chunk) > max_bytes:
                    raise ValueError("Download exceeds declared size")
                if first_chunk_seconds is None:
                    first_chunk_seconds = time.perf_counter() - started
                stream.write(chunk)
                if digest is not None:
                    digest.update(chunk)
                size += len(chunk)
        if digest is not None and digest.hexdigest() != expected_sha256:
            raise ValueError("Downloaded content checksum mismatch")
        if kind is not None:
            from runtime.utils.media import media_suffix
            with temp.open("rb") as stream:
                media_suffix(stream.read(32), kind)
        if refuse_existing and destination.exists():
            raise FileExistsError(destination)
        os.replace(temp, destination)
        seconds = time.perf_counter() - started
        return {"downloaded_bytes": size, "seconds": seconds,
                "first_chunk_seconds": first_chunk_seconds,
                "mib_per_second": size / (1024 * 1024 * seconds) if seconds else 0}
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


@contextmanager
def file_lock(directory, *, filename=".runner.lock", timeout=0):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / filename).open("a+b") as stream:
        if stream.seek(0, 2) == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(f"Another runner holds {directory}") from exc
                time.sleep(min(0.01, remaining))
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
