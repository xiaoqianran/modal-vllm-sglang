"""Offline tests. No HF calls, model deployment, or GPU usage."""
import hashlib
from pathlib import Path

import pytest
from PIL import Image

from runtime.archive import ArchivePipeline, ArchiveSettings
from runtime.archive.huggingface import public_url
from runtime.archive.operations import prepare_media


def fixture_png(path: Path):
    image = Image.new("RGB", (128, 128))
    px = image.load()
    for y in range(128):
        for x in range(128):
            px[x, y] = (y // 8 * 12, x // 8 * 12, 60)
    image.save(path)


def test_adaptive_webp_preserves_pixels(tmp_path):
    path = tmp_path / "test.png"
    fixture_png(path)
    prepared = prepare_media(path, tmp_path / "temp", min_saving=0.0)
    assert prepared.original_size == path.stat().st_size
    assert prepared.stored_size <= prepared.original_size
    if prepared.encoding == "webp_lossless":
        with Image.open(path) as a, Image.open(prepared.upload_file) as b:
            assert a.tobytes() == b.tobytes()
    assert path.exists()


def test_dry_run_no_upload_and_source_preserved(tmp_path):
    path = tmp_path / "test.png"
    fixture_png(path)
    before = hashlib.sha256(path.read_bytes()).digest()
    pipeline = ArchivePipeline(ArchiveSettings("example/art", "project01"))
    record = pipeline.run(path)
    assert record["status"] == "planned"
    assert record["verification"] == "not_uploaded"
    assert record["url"].startswith("https://huggingface.co/buckets/example/art/resolve/")
    assert hashlib.sha256(path.read_bytes()).digest() == before


def test_fake_upload_and_manifest(tmp_path):
    path = tmp_path / "test.png"
    fixture_png(path)
    class FakeProvider:
        def __init__(self):
            self.calls = []
        def upload(self, local_file, remote_path):
            self.calls.append(("upload", remote_path, local_file.stat().st_size))
            return "https://example.invalid/" + remote_path
        def verify(self, local_file, remote_path, mode):
            self.calls.append(("verify", remote_path, mode))
            return mode
    fake = FakeProvider()
    manifest = tmp_path / "record.json"
    record = ArchivePipeline(ArchiveSettings("example/art", "a01", verify="sha256"),
                             provider=fake).run(path, upload=True, manifest=manifest)
    assert record["status"] == "archived"
    assert record["verification"] == "sha256"
    assert [x[0] for x in fake.calls] == ["upload", "verify"]
    assert manifest.is_file()


def test_mp4_kept_unchanged(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"test MP4 placeholder")
    media = prepare_media(path, tmp_path / "work")
    assert media.upload_file == path
    assert media.encoding == "original"


def test_invalid_inputs(tmp_path):
    path = tmp_path / "empty.png"
    path.touch()
    with pytest.raises(ValueError, match="Empty"):
        prepare_media(path, tmp_path)
    with pytest.raises(ValueError):
        public_url("example/art", "../escape.png")
    with pytest.raises(ValueError):
        ArchivePipeline(ArchiveSettings("example/art", "../bad"))


def test_default_hf_bucket_cli_constant():
    from runtime.archive.config import DEFAULT_ASSET_BUCKET
    from runtime.delivery import normalize_delivery
    assert DEFAULT_ASSET_BUCKET == "seachen/modal-vllm-sglang-assets"
    assert normalize_delivery({"provider": "huggingface"})["bucket"] == DEFAULT_ASSET_BUCKET
