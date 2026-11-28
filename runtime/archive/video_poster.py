"""Verified, optional WebP posters for archived MP4 assets."""
from __future__ import annotations

import json
import re
import subprocess
from hashlib import sha256
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from runtime.archive.operations import verified_volume_copy
from runtime.archive.huggingface import public_url
from runtime.archive.preview import DerivativeInputError

POLICY = "webp-640-q82-v1"
MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_POSTER_BYTES = 512 * 1024
HEX = re.compile(r"[0-9a-f]{64}")


def index_path(source_digest):
    if not isinstance(source_digest, str) or not HEX.fullmatch(source_digest):
        raise ValueError("Invalid source digest")
    return f"indexes/video-poster/{POLICY}/{source_digest[:2]}/{source_digest}.json"


def prepare_video_poster(source, workspace):
    """Extract one representative frame and encode a bounded 640px WebP."""
    from PIL import Image, ImageOps

    source, workspace = Path(source), Path(workspace)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=20, check=True)
    try:
        duration = max(0.0, float(probe.stdout.strip()))
    except ValueError:
        duration = 0.0
    seek = min(duration * 0.2, 1.0) if duration else 0.0
    frame = workspace / "poster.png"
    last_error = None
    for position in dict.fromkeys((seek, 0.0)):
        try:
            subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{position:.3f}",
                 "-i", str(source), "-frames:v", "1",
                 "-vf", "scale=640:640:force_original_aspect_ratio=decrease:force_divisible_by=2",
                 str(frame)],
                capture_output=True, timeout=45, check=True)
            if frame.is_file() and frame.stat().st_size:
                break
        except subprocess.CalledProcessError as exc:
            reason = (exc.stderr or b"").lower()
            if not any(message in reason for message in (
                b"invalid data", b"no streams", b"does not contain any stream",
                b"error while decoding", b"invalid nal", b"could not find codec parameters",
            )):
                raise
            last_error = exc
    else:
        raise DerivativeInputError("No decodable video frame") from last_error

    with Image.open(frame) as opened:
        image = ImageOps.exif_transpose(opened)
        image.thumbnail((640, 640), Image.Resampling.LANCZOS)
        image = image.convert("RGB")
        image.load()
    poster = workspace / "poster.webp"
    image.save(poster, "WEBP", quality=82, method=4)
    size = poster.stat().st_size
    if not 0 < size <= MAX_POSTER_BYTES:
        raise DerivativeInputError("Video poster exceeds size limit")
    digest = sha256(poster.read_bytes()).hexdigest()
    return poster, {"sha256": digest, "bytes": size, "dimensions": list(image.size),
                    "path": f"thumbnails/sha256/{digest[:2]}/{digest}.webp"}


def poster_mapping(source_digest, source_size, poster):
    return json.dumps({"source_sha256": source_digest, "source_bytes": source_size,
                       "kind": "video", "policy": POLICY, "poster": poster},
                      sort_keys=True, separators=(",", ":")).encode()


def lookup_video_poster(provider, source_digest, source_size):
    """Reuse only a matching, fully verified poster and policy index."""
    try:
        with urlopen(Request(public_url(provider.bucket, index_path(source_digest)),
                             headers={"Accept-Encoding": "identity"}), timeout=8) as response:
            payload = response.read(4097)
        if len(payload) > 4096:
            return None
        row = json.loads(payload)
        poster = row["poster"]
        digest = poster["sha256"]
        if (row.get("source_sha256") != source_digest or row.get("source_bytes") != source_size
                or row.get("kind") != "video" or row.get("policy") != POLICY
                or not HEX.fullmatch(digest) or poster["path"] != f"thumbnails/sha256/{digest[:2]}/{digest}.webp"
                or poster["url"] != public_url(provider.bucket, poster["path"])
                or poster.get("verification") != "sha256"
                or type(poster.get("bytes")) is not int or not 0 < poster["bytes"] <= MAX_POSTER_BYTES):
            return None
        dims = poster.get("dimensions")
        if len(dims) != 2 or any(type(x) is not int or x <= 0 or x > 640 for x in dims):
            return None
        provider.verify_digest(poster["path"], poster["bytes"], digest)
        return poster
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError, OverflowError):
        return None


def backfill_video_batch(bucket, receipts, *, volume_root=None, local_inputs=None):
    """Stream verified HF MP4s, create posters on CPU, and batch publish."""
    from tempfile import TemporaryDirectory
    from runtime.archive.huggingface import HuggingFaceBucket

    if not 1 <= len(receipts) <= 4:
        raise ValueError("Video poster batch count")
    provider = HuggingFaceBucket(bucket)
    entries, mappings, prepared, results = {}, {}, {}, {}
    with TemporaryDirectory(prefix="hf-video-poster-") as tmp:
        for i, row in enumerate(receipts):
            url = row.get("url")
            try:
                size, digest = row["stored_bytes"], row["stored_sha256"]
                if (row.get("bucket") != bucket or row.get("kind") != "video"
                        or row.get("status") != "archived" or row.get("verification") != "sha256"
                        or url != public_url(bucket, row["path"]) or type(size) is not int
                        or not 0 < size <= MAX_SOURCE_BYTES or not HEX.fullmatch(digest)
                        or PurePosixPath(row["path"]).suffix.lower() != ".mp4"):
                    raise DerivativeInputError("Invalid video receipt")
                from runtime.archive.works import derivative_path
                sibling = derivative_path(row["path"])
                cached = None if sibling else lookup_video_poster(provider, digest, size)
                if cached:
                    results[url] = {"source_sha256": digest, "status": "poster",
                                    "poster": cached, "reused": True}
                    continue
                work = Path(tmp) / str(i)
                work.mkdir()
                source = work / "source.mp4"
                retained = (local_inputs or {}).get((bucket, digest, size))
                if retained is not None and Path(retained).is_file() and Path(retained).stat().st_size == size:
                    source = Path(retained)
                elif not verified_volume_copy(volume_root, row, source):
                    received, actual = 0, sha256()
                    with urlopen(Request(url, headers={"Accept-Encoding": "identity"}), timeout=60) as response, source.open("wb") as out:
                        for block in iter(lambda: response.read(1024 * 1024), b""):
                            received += len(block)
                            if received > size:
                                raise ValueError("Video source size mismatch")
                            actual.update(block)
                            out.write(block)
                    if received != size or actual.hexdigest() != digest:
                        raise ValueError("Video source SHA-256 mismatch")
                try:
                    file, poster = prepare_video_poster(source, work)
                except subprocess.CalledProcessError as exc:
                    detail = exc.stderr or ""
                    if isinstance(detail, bytes):
                        detail = detail.decode(errors="replace")
                    if any(s in detail.lower() for s in ("invalid data", "moov atom not found",
                                                        "error reading header")):
                        raise DerivativeInputError("Video decoder rejected input") from exc
                    raise
                if sibling:
                    poster = {**poster, "path": sibling}
                poster = {**poster, "url": public_url(bucket, poster["path"]),
                          "verification": "sha256"}
                entries.setdefault(poster["path"], (file, poster["sha256"]))
                if not sibling:
                    mappings.setdefault(poster["path"], {})[index_path(digest)] = poster_mapping(digest, size, poster)
                prepared[url] = (digest, poster)
            except DerivativeInputError as exc:
                results[url] = {"error": str(exc), "invalid_input": True}
            except Exception as exc:
                results[url] = {"error": type(exc).__name__}
        outcomes = provider.publish_many(entries, indexes=mappings) if entries else {}
        for url, (digest, poster) in prepared.items():
            result = outcomes.get(poster["path"])
            if isinstance(result, tuple) and result[1] == "sha256":
                results[url] = {"source_sha256": digest, "status": "poster",
                                "poster": {**poster, "url": result[0], "verification": "sha256"}}
            else:
                results[url] = {"error": "Poster verification failed"}
    return results
