"""Full-resolution, independently verified gallery previews."""
from pathlib import Path
from math import log10
from runtime.utils.files import sha256_file
from runtime.archive.huggingface import public_url

def prepare_preview(source, workspace, stored_size):
    from PIL import Image, ImageOps, ImageChops, ImageStat
    from io import BytesIO
    with Image.open(source) as opened:
        if getattr(opened, "is_animated", False) or opened.width * opened.height > 24_000_000:
            return None
        image = ImageOps.exif_transpose(opened)
        if image.mode != "RGB" or image.info.get("icc_profile"):
            return None
        image.load()
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    # A conservative full-image RGB error gate; no additional CPU dependencies.
    for quality in (92, 95):
        buf = BytesIO()
        image.save(buf, format="WEBP", quality=quality, method=4)
        size = buf.tell()
        if size >= stored_size * 0.8:
            return None
        with Image.open(buf) as decoded:
            mse = sum(v*v for v in ImageStat.Stat(ImageChops.difference(image, decoded.convert("RGB"))).rms) / 3
        psnr = 10 * log10(65025 / mse) if mse else 99
        if psnr < 40:
            continue
        file = workspace / "preview.webp"
        file.write_bytes(buf.getvalue())
        digest = sha256_file(file)
        return file, {"path": f"previews/sha256/{digest[:2]}/{digest}.webp",
                      "sha256": digest, "bytes": size, "dimensions": list(image.size),
                      "quality": quality, "psnr": round(psnr, 3)}
    return None

def preview_url(receipt):
    """Accept only verified, content-addressed previews in the same bucket."""
    try:
        row = receipt["preview"]
        digest = row["sha256"]
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            return ""
        if row["verification"] != "sha256" or row["path"] != f"previews/sha256/{digest[:2]}/{digest}.webp":
            return ""
        url = public_url(receipt["bucket"], row["path"])
        return url if url == row["url"] else ""
    except (KeyError, TypeError, ValueError, AttributeError):
        return ""


def verified_preview(receipt, index=None):
    """Prefer receipt metadata, otherwise an independently backfilled preview."""
    url = preview_url(receipt)
    if url:
        return url
    row = (index or {}).get(receipt.get("url"), {})
    if row.get("source_sha256") != receipt.get("stored_sha256"):
        return ""
    return preview_url({"bucket": receipt.get("bucket"), "preview": row.get("preview")})


def preview_index_path(digest):
    from PIL import __version__, features
    from hashlib import sha256
    import re
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Invalid source digest")
    policy = sha256(f"webp-full-v1:{__version__}:{features.version('webp')}".encode()).hexdigest()
    return f"indexes/preview/{policy}/{digest[:2]}/{digest}.json"


def preview_mapping(digest, size, preview):
    import json
    return json.dumps({"source_sha256": digest, "source_bytes": size,
                       "preview": preview}, sort_keys=True, separators=(",", ":")).encode()


def lookup_preview(provider, digest, size):
    """A missing/corrupt optional index is a cache miss, never a delivery error."""
    import json
    from urllib.request import Request, urlopen
    try:
        path = preview_index_path(digest)
        with urlopen(Request(public_url(provider.bucket, path),
                             headers={"Accept-Encoding": "identity"}), timeout=8) as response:
            data = response.read(4097)
        if len(data) > 4096:
            return None
        row = json.loads(data)
        preview = row["preview"]
        if row["source_sha256"] != digest or row["source_bytes"] != size:
            return None
        if (not preview_url({"bucket": provider.bucket, "preview": preview})
                or type(preview["bytes"]) is not int or not 0 < preview["bytes"] < size * .8
                or preview["quality"] not in (92, 95)
                or not 40 <= preview["psnr"] <= 99):
            return None
        dims = preview["dimensions"]
        if len(dims) != 2 or any(type(x) is not int or x <= 0 for x in dims):
            return None
        provider.verify_digest(preview["path"], preview["bytes"], preview["sha256"])
        return preview
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError, OverflowError):
        return None


def backfill_batch(bucket, receipts):
    """CPU-only: stream verified HF originals, publish previews in one batch."""
    from tempfile import TemporaryDirectory
    from urllib.request import Request, urlopen
    from runtime.archive.huggingface import HuggingFaceBucket
    from hashlib import sha256
    from pathlib import PurePosixPath

    if not 1 <= len(receipts) <= 4:
        raise ValueError("Preview batch count")
    provider = HuggingFaceBucket(bucket)
    entries, prepared, results, mappings = {}, {}, {}, {}
    with TemporaryDirectory(prefix="hf-preview-") as tmp:
        for i, row in enumerate(receipts):
            url = row.get("url")
            try:
                size, digest = row["stored_bytes"], row["stored_sha256"]
                if (row.get("bucket") != bucket or row.get("kind") != "image"
                    or row.get("status") != "archived" or row.get("verification") != "sha256"
                    or url != public_url(bucket, row["path"]) or type(size) is not int
                    or not 0 < size <= 64 * 1024 * 1024 or len(digest) != 64):
                    raise ValueError("Invalid source receipt")
                suffix = PurePosixPath(row["path"]).suffix.lower()
                if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
                    raise ValueError("Unsupported preview source")
                cached = lookup_preview(provider, digest, size)
                if cached:
                    results[url] = {"source_sha256": digest, "status": "preview", "preview": cached, "reused": True}
                    continue
                work = Path(tmp) / str(i)
                work.mkdir()
                source = work / ("source" + suffix)
                received, h = 0, sha256()
                with urlopen(Request(url, headers={"Accept-Encoding": "identity"}), timeout=60) as response, source.open("wb") as out:
                    for chunk in iter(lambda: response.read(1024 * 1024), b""):
                        received += len(chunk)
                        if received > size:
                            raise ValueError("Source size mismatch")
                        h.update(chunk)
                        out.write(chunk)
                if received != size or h.hexdigest() != digest:
                    raise ValueError("Source integrity mismatch")
                candidate = prepare_preview(source, work, size)
                if candidate is None:
                    results[url] = {"source_sha256": digest, "status": "original"}
                    continue
                file, preview = candidate
                entries.setdefault(preview["path"], (file, preview["sha256"]))
                preview = {**preview, "url": public_url(bucket, preview["path"]), "verification": "sha256"}
                mappings.setdefault(preview["path"], {})[preview_index_path(digest)] = preview_mapping(digest, size, preview)
                prepared[url] = (digest, preview)
            except Exception as exc:
                results[url] = {"error": type(exc).__name__}
        outcomes = provider.publish_many(entries, indexes=mappings) if entries else {}
        for url, (digest, preview) in prepared.items():
            outcome = outcomes.get(preview["path"])
            if isinstance(outcome, tuple) and outcome[1] == "sha256":
                results[url] = {"source_sha256": digest, "status": "preview",
                                "preview": {**preview, "url": outcome[0], "verification": "sha256"}}
            else:
                results[url] = {"error": "Preview verification failed"}
    return results
