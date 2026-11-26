"""Hugging Face Storage Bucket adapter, imported only when publishing."""
from __future__ import annotations

import os
import re
import hashlib
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from runtime.utils.files import sha256_file

_BUCKET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SAFE = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$")


def public_url(bucket: str, remote_path: str) -> str:
    if (not _BUCKET.fullmatch(bucket) or not _SAFE.fullmatch(remote_path) or ".." in remote_path.split("/") or "." in remote_path.split("/")):
        raise ValueError("Unsafe bucket or remote path")
    owner, name = bucket.split("/", 1)
    return (
        f"https://huggingface.co/buckets/{quote(owner, safe='')}/"
        f"{quote(name, safe='')}/resolve/{quote(remote_path, safe='/')}"
    )


def configured_hf_token():
    """Use env/.env or an existing Hugging Face CLI login; never print tokens."""
    from dotenv import load_dotenv
    from huggingface_hub import get_token
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
    return os.getenv("HF_TOKEN") or get_token()


class HuggingFaceBucket:
    def __init__(self, bucket: str, token: str | None = None):
        if not _BUCKET.fullmatch(bucket):
            raise ValueError("Bucket must be owner/name")
        self.bucket = bucket
        self.token = token or configured_hf_token()
        if not self.token:
            raise RuntimeError("Hugging Face login or HF_TOKEN is required for upload")

    def upload(self, local_file: Path, remote_path: str) -> str:
        from huggingface_hub import batch_bucket_files
        # Remote filenames are content-addressed by ArchivePipeline.
        public_url(self.bucket, remote_path)
        batch_bucket_files(self.bucket, add=[(str(local_file), remote_path)], token=self.token)
        return public_url(self.bucket, remote_path)

    def verify(self, local_file: Path, remote_path: str, mode: str, *,
               expected_sha256: str | None = None) -> str:
        url = public_url(self.bucket, remote_path)
        size = local_file.stat().st_size
        if mode == "size":
            from huggingface_hub import get_bucket_file_metadata
            metadata = get_bucket_file_metadata(self.bucket, remote_path, token=self.token)
            if metadata.size != size:
                raise RuntimeError("HF remote size differs from local media")
            return "size"
        if mode != "sha256":
            raise ValueError(f"Unsupported verification mode: {mode}")
        # Read the same public URL as the HTML gallery. Never relay credentials
        # to redirects or write a verification copy to disk.
        expected = expected_sha256 or sha256_file(local_file)
        digest = hashlib.sha256()
        received = 0
        request = Request(url, headers={"Accept-Encoding": "identity"})
        with urlopen(request, timeout=60) as response:
            if response.status != 200:
                raise RuntimeError("HF verification requires a full response")
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                received += len(chunk)
                if received > size:
                    raise RuntimeError("HF remote size differs from local media")
                digest.update(chunk)
        if received != size:
            raise RuntimeError("HF remote size differs from local media")
        if digest.hexdigest() != expected:
            raise RuntimeError("HF remote SHA256 differs from local media")
        return "sha256"
