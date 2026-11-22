"""Hugging Face Storage Bucket adapter, imported only when publishing."""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from urllib.parse import quote

from .operations import sha256_file

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

    def verify(self, local_file: Path, remote_path: str, mode: str) -> str:
        from huggingface_hub import get_bucket_file_metadata
        metadata = get_bucket_file_metadata(self.bucket, remote_path, token=self.token)
        if metadata.size != local_file.stat().st_size:
            raise RuntimeError("HF remote size differs from local media")
        if mode == "size":
            return "size"
        if mode != "sha256":
            raise ValueError(f"Unsupported verification mode: {mode}")
        from huggingface_hub import download_bucket_files
        with tempfile.TemporaryDirectory(prefix="hf-archive-verify-") as directory:
            restored = Path(directory) / "restored.bin"
            download_bucket_files(self.bucket, files=[(remote_path, str(restored))],
                                  token=self.token)
            if sha256_file(restored) != sha256_file(local_file):
                raise RuntimeError("HF remote SHA256 differs from local media")
        return "sha256"
