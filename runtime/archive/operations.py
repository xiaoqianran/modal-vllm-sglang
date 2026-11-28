"""Pure, local media preparation and manifest functions."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime.utils.files import atomic_json, sha256_file

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXTS = {".mp4"}


@dataclass(frozen=True)
class PreparedMedia:
    source: Path
    upload_file: Path
    kind: str
    original_size: int
    stored_size: int
    original_sha256: str
    stored_sha256: str
    encoding: str
    remote_only: bool = False


def prepare_media(source: str | Path, workspace: Path, policy: str = "adaptive_lossless",
                  min_saving: float = 0.05, *, expected_sha256: str | None = None,
                  lookup=None, _verified_snapshot=False) -> PreparedMedia:
    """Preserve the input. Only encode 8-bit RGB/RGBA files with no metadata."""
    path = Path(source).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    size = path.stat().st_size
    if size <= 0:
        raise ValueError("Empty media file")
    suffix = path.suffix.lower()
    if suffix not in IMAGE_EXTS | VIDEO_EXTS:
        raise ValueError(f"Unsupported media extension: {suffix}")
    if policy not in {"none", "adaptive_lossless"}:
        raise ValueError(f"Unsupported compression policy: {policy}")
    if not 0 <= min_saving < 1:
        raise ValueError("min_saving must be in [0, 1)")
    # Only the private snapshot produced by verified_volume_copy may use this path.
    if _verified_snapshot and expected_sha256 is None:
        raise ValueError("Verified snapshot requires digest")
    original_sha256 = expected_sha256 if _verified_snapshot else sha256_file(path)
    if expected_sha256 is not None and original_sha256 != expected_sha256:
        raise ValueError("Source artifact checksum mismatch")
    kind = "image" if suffix in IMAGE_EXTS else "video"
    selected = path
    encoding = "original"

    if suffix == ".png" and policy == "adaptive_lossless":
        if lookup is not None:
            original = PreparedMedia(path, path, kind, size, size,
                                     original_sha256, original_sha256, "original")
            cached = lookup(original, min_saving)
            if cached is not None:
                return cached
        from PIL import Image, ImageChops

        with Image.open(path) as image:
            # Never re-encode existing WebP, animated PNG, metadata, palette,
            # or high-bit-depth originals. Lossless quality is encoder effort.
            if image.mode in {"RGB", "RGBA"} and not image.info and not getattr(image, "is_animated", False):
                workspace.mkdir(parents=True, exist_ok=True)
                candidate = workspace / "compressed.webp"
                image.save(candidate, format="WEBP", lossless=True, method=2,
                           quality=20, exact=True)
                if candidate.stat().st_size < size * (1 - min_saving):
                    with Image.open(candidate) as decoded:
                        if decoded.size != image.size:
                            raise RuntimeError("Decoded WebP dimensions differ from source")
                        # Avoid two RGBA copies when the modes already match.
                        actual = decoded if decoded.mode == image.mode else decoded.convert("RGBA")
                        expected = image if decoded.mode == image.mode else image.convert("RGBA")
                        if ImageChops.difference(expected, actual).getbbox(alpha_only=False) is not None:
                            raise RuntimeError("Decoded WebP pixels differ from source")
                    selected = candidate
                    encoding = "webp_lossless"

    return PreparedMedia(
        source=path, upload_file=selected, kind=kind,
        original_size=size, stored_size=selected.stat().st_size,
        original_sha256=original_sha256,
        stored_sha256=original_sha256 if selected == path else sha256_file(selected),
        encoding=encoding,
    )


def describe(media: PreparedMedia, bucket: str, remote_path: str, url: str,
             verification: str, status: str) -> dict[str, Any]:
    return {
        "status": status, "provider": "huggingface_bucket",
        "bucket": bucket, "path": remote_path, "url": url,
        "kind": media.kind, "name": media.source.name,
        "original_bytes": media.original_size, "stored_bytes": media.stored_size,
        "saved_percent": round(100 * (1 - media.stored_size / media.original_size), 2),
        "compression": media.encoding, "original_sha256": media.original_sha256,
        "stored_sha256": media.stored_sha256, "verification": verification,
    }


def write_record(path: str | Path, record: dict[str, Any]) -> None:
    atomic_json(path, record)


def verified_volume_copy(root, receipt, destination):
    """Snapshot the exact archived bytes for decoding, falling back to HF on a miss."""
    from hashlib import sha256
    from runtime.delivery.cloud_cpu import mounted_source

    artifact = receipt.get("source_artifact")
    if root is None or not artifact or artifact.get("sha256") != receipt["stored_sha256"]:
        return False
    expected_size = receipt["stored_bytes"]
    if artifact.get("size") != expected_size:
        return False
    try:
        path = mounted_source(root, artifact)
        digest = sha256()
        received = 0
        with path.open("rb") as source, destination.open("wb") as target:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                received += len(block)
                if received > expected_size:
                    return False
                target.write(block)
                digest.update(block)
        return received == expected_size and digest.hexdigest() == receipt["stored_sha256"]
    except (OSError, ValueError):
        return False
