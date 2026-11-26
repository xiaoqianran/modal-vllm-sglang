"""Persistent, optional encoding index in the same trusted HF bucket."""
from dataclasses import replace
from hashlib import sha256
import json
import re
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .huggingface import public_url
from .pipeline import asset_path

# Bump whenever the lossless eligibility or encoder parameters change.
ENCODING_SCHEMA = "png-webp-lossless-v1"
MAX_RECORD_BYTES = 4096


def mapping_path(media, min_saving):
    from PIL import __version__, features
    policy = json.dumps([ENCODING_SCHEMA, __version__, features.version("webp"),
                         float(min_saving)], separators=(",", ":"))
    version = sha256(policy.encode()).hexdigest()
    digest = media.original_sha256
    return f"indexes/encoding/{version}/{digest[:2]}/{digest}.json"


def mapping_record(media):
    return {"original_sha256": media.original_sha256,
            "original_size": media.original_size,
            "stored_sha256": media.stored_sha256,
            "stored_size": media.stored_size, "encoding": media.encoding}


def lookup_encoding(provider, original, min_saving):
    """Validate the index shape and full remote bytes; a missing index is a miss."""
    path = mapping_path(original, min_saving)
    try:
        with urlopen(Request(public_url(provider.bucket, path),
                             headers={"Accept-Encoding": "identity"}), timeout=10) as response:
            if response.status != 200:
                return None
            data = response.read(MAX_RECORD_BYTES + 1)
        if len(data) > MAX_RECORD_BYTES:
            return None
        row = json.loads(data)
    except (OSError, ValueError):
        # Index availability is optional, unlike actual asset verification.
        return None
    if not isinstance(row, dict):
        return None
    digest, size, encoding = row.get("stored_sha256"), row.get("stored_size"), row.get("encoding")
    if (row.get("original_sha256") != original.original_sha256
            or row.get("original_size") != original.original_size
            or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or type(size) is not int or size <= 0):
        return None
    if encoding == "original":
        if digest != original.original_sha256 or size != original.original_size:
            return None
    elif encoding == "webp_lossless":
        if size >= original.original_size * (1 - min_saving):
            return None
    else:
        return None
    media = replace(original, stored_size=size, stored_sha256=digest,
                    encoding=encoding, remote_only=True)
    try:
        provider.verify_digest(asset_path(media), size, digest)
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    return media


def mapping_bytes(media):
    """Locally verified encoding metadata; remote media still needs full readback."""
    return json.dumps(mapping_record(media), sort_keys=True, separators=(",", ":")).encode()
