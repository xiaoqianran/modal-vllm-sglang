"""Hugging Face Storage Bucket adapter, imported only when publishing."""
from __future__ import annotations

import os
import re
import hashlib
from pathlib import Path
from urllib.parse import quote
from urllib.error import HTTPError
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

    def upload_many(self, entries):
        """One SDK submission for distinct missing content addresses."""
        from huggingface_hub import batch_bucket_files
        for path in entries:
            public_url(self.bucket, path)
        batch_bucket_files(self.bucket, add=[(str(file), path) for path, (file, _) in entries.items()],
                           token=self.token)

    def publish_many(self, entries, *, metrics=None):
        """path -> (file, expected SHA); independently verify each result.

        Even after a partially failed SDK submission, discover successful
        objects by full readback. Missing or ambiguous objects remain errors.
        """
        from concurrent.futures import ThreadPoolExecutor
        from time import perf_counter

        metrics = {} if metrics is None else metrics

        def check(item):
            path, (file, digest) = item
            try:
                self.verify(file, path, "sha256", expected_sha256=digest)
                return path, (public_url(self.bucket, path), "sha256", True)
            except Exception as exc:
                return path, exc

        with ThreadPoolExecutor(max_workers=4) as pool:
            started = perf_counter()
            results = dict(pool.map(check, entries.items()))
            missing = {p: entries[p] for p, r in results.items()
                       if isinstance(r, HTTPError) and r.code == 404}
            metrics.update(lookup_ms=round((perf_counter() - started)*1000, 2),
                           existing_hits=sum(isinstance(r, tuple) for r in results.values()),
                           upload_calls=int(bool(missing)),
                           submitted_bytes=sum(file.stat().st_size for file, _ in missing.values()))
            if missing:
                started = perf_counter()
                try:
                    self.upload_many(missing)
                except Exception:
                    # Non-transactional API: do not blindly resubmit the batch.
                    metrics["upload_batch_error"] = True
                metrics["upload_ms"] = round((perf_counter() - started)*1000, 2)
                started = perf_counter()
                for path, result in pool.map(check, missing.items()):
                    results[path] = (*result[:2], False) if isinstance(result, tuple) else result
                metrics["verify_ms"] = round((perf_counter() - started)*1000, 2)
        return results

    def publish(self, local_file: Path, remote_path: str, mode: str, *,
                expected_sha256: str) -> tuple[str, str, bool]:
        """Reuse only a publicly readable object with the exact stored bytes."""
        url = public_url(self.bucket, remote_path)
        try:
            verification = self.verify(local_file, remote_path, "sha256",
                                       expected_sha256=expected_sha256)
        except HTTPError as exc:
            # Missing object is the only condition that permits publication.
            # Permissions, transport failures and corrupted content fail closed.
            if exc.code != 404:
                raise
        else:
            return url, verification, True
        self.upload(local_file, remote_path)
        verification = self.verify(local_file, remote_path, mode,
                                   expected_sha256=expected_sha256)
        return url, verification, False

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
