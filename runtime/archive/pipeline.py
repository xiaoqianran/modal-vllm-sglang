"""Readable main flow. This is OPT-IN and never changes normal outputs delivery."""
from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .huggingface import HuggingFaceBucket, public_url
from .operations import PreparedMedia, describe, prepare_media, write_record


class ArchiveProvider(Protocol):
    def publish(self, local_file: Path, remote_path: str, mode: str, *,
                expected_sha256: str) -> tuple[str, str, bool]: ...


@dataclass(frozen=True)
class ArchiveSettings:
    bucket: str
    project: str
    compression: str = "adaptive_lossless"
    min_saving: float = 0.05
    verify: str = "size"


class ArchivePipeline:
    def __init__(self, settings: ArchiveSettings, provider: ArchiveProvider | None = None):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", settings.project):
            raise ValueError("Invalid project name")
        if settings.verify not in {"size", "sha256"}:
            raise ValueError("verify must be size or sha256")
        public_url(settings.bucket, "check")
        self.settings = settings
        self.provider = provider  # Fake provider for offline tests or HF on real upload.

    def run(self, source: str | Path, *, upload: bool = False,
            manifest: str | Path | None = None, expected_sha256: str | None = None) -> dict:
        """Prepare -> publish -> verify -> record; never mutate original outputs."""
        with tempfile.TemporaryDirectory(prefix="hf-archive-") as workdir:
            media = prepare_media(source, Path(workdir), self.settings.compression,
                                  self.settings.min_saving, expected_sha256=expected_sha256)
            remote_path = self.remote_path(media)
            url = public_url(self.settings.bucket, remote_path)
            verification = "not_uploaded"
            reused = False
            if upload:
                provider = self.provider or HuggingFaceBucket(self.settings.bucket)
                url, verification, reused = provider.publish(
                    media.upload_file, remote_path, self.settings.verify,
                    expected_sha256=media.stored_sha256)
            record = describe(media, self.settings.bucket, remote_path, url,
                              verification, "archived" if upload else "planned")
            record["reused"] = reused
            if manifest is not None:
                write_record(manifest, record)
            return record

    def remote_path(self, media: PreparedMedia) -> str:
        return asset_path(media)


def asset_path(media: PreparedMedia) -> str:
    """Global content address within the selected bucket, independent of project."""
    digest = media.stored_sha256
    suffix = media.upload_file.suffix.lower()
    if suffix == ".jpeg":
        suffix = ".jpg"
    return f"assets/sha256/{digest[:2]}/{digest}{suffix}"
