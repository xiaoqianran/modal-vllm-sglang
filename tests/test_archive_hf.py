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
        def publish(self, local_file, remote_path, mode, *, expected_sha256):
            assert expected_sha256 == hashlib.sha256(local_file.read_bytes()).hexdigest()
            self.calls.append(("publish", remote_path, mode))
            return "https://example.invalid/" + remote_path, mode, False
    fake = FakeProvider()
    manifest = tmp_path / "record.json"
    record = ArchivePipeline(ArchiveSettings("example/art", "a01", verify="sha256"),
                             provider=fake).run(path, upload=True, manifest=manifest)
    assert record["status"] == "archived"
    assert record["verification"] == "sha256"
    assert [x[0] for x in fake.calls] == ["publish"]
    assert record["reused"] is False
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


def test_original_hashed_once_and_reused(tmp_path):
    from unittest.mock import patch
    from runtime.archive.operations import sha256_file
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"media" * 100)
    with patch("runtime.archive.operations.sha256_file", wraps=sha256_file) as digest:
        media = prepare_media(path, tmp_path / "work", expected_sha256=sha256_file(path))
        assert media.original_sha256 == media.stored_sha256
        digest.assert_called_once_with(path.resolve())


@pytest.mark.parametrize("payload,error", [(b"original", None), (b"tampered", "SHA256"),
                                          (b"short", "size"), (b"original-extra", "size")])
def test_public_remote_stream_verification(tmp_path, payload, error):
    from io import BytesIO
    from unittest.mock import patch
    from runtime.archive.huggingface import HuggingFaceBucket
    class Response(BytesIO):
        status = 200
        def read(self, size=-1):
            assert size == 1024 * 1024
            return super().read(size)
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"original")
    bucket = HuggingFaceBucket("example/art", token="test")
    expected = hashlib.sha256(b"original").hexdigest()
    with patch("runtime.archive.huggingface.urlopen", return_value=Response(payload)) as request, \
         patch("runtime.archive.huggingface.sha256_file", side_effect=AssertionError("repeated hash")):
        if error:
            with pytest.raises(RuntimeError, match=error):
                bucket.verify(path, "projects/example.mp4", "sha256", expected_sha256=expected)
        else:
            assert bucket.verify(path, "projects/example.mp4", "sha256", expected_sha256=expected) == "sha256"
        assert request.call_args.args[0].get_header("Authorization") is None


def test_source_checksum_rejected_before_upload(tmp_path):
    from unittest.mock import Mock
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"wrong")
    provider = Mock()
    pipeline = ArchivePipeline(ArchiveSettings("example/art", "p", compression="none"), provider=provider)
    with pytest.raises(ValueError, match="checksum"):
        pipeline.run(path, upload=True, expected_sha256=hashlib.sha256(b"right").hexdigest())
    provider.publish.assert_not_called()


@pytest.mark.parametrize("case", ["hit", "missing", "forbidden", "corrupt", "timeout"])
def test_content_addressed_publish(tmp_path, case):
    from unittest.mock import patch
    from urllib.error import HTTPError
    from runtime.archive.huggingface import HuggingFaceBucket
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"media")
    digest = hashlib.sha256(b"media").hexdigest()
    bucket = HuggingFaceBucket("example/art", token="test")
    remote = f"projects/p/videos/{digest}.mp4"
    url = public_url(bucket.bucket, remote)
    err = {
        "forbidden": HTTPError(url, 403, "denied", {}, None),
        "corrupt": RuntimeError("SHA256 differs"),
        "timeout": TimeoutError("network timeout"),
    }
    side = ["sha256"] if case == "hit" else (
        [HTTPError(url, 404, "missing", {}, None), "sha256"] if case == "missing" else err[case])
    with patch.object(bucket, "verify", side_effect=side) as verify, patch.object(bucket, "upload") as upload:
        if case in err:
            with pytest.raises(type(err[case])):
                bucket.publish(p, remote, "sha256", expected_sha256=digest)
            upload.assert_not_called()
        else:
            assert bucket.publish(p, remote, "sha256", expected_sha256=digest) == (url, "sha256", case == "hit")
            if case == "missing":
                upload.assert_called_once_with(p, remote)
                assert verify.call_count == 2
            else:
                upload.assert_not_called()
                assert verify.call_count == 1


def test_existing_webp_is_not_reencoded(tmp_path):
    from unittest.mock import patch
    path = tmp_path / "image.webp"
    Image.new("RGB", (64, 64), "red").save(path, lossless=True)
    with patch("PIL.Image.open", side_effect=AssertionError("existing WebP must be preserved")):
        media = prepare_media(path, tmp_path / "work")
    assert media.upload_file == path
    assert media.original_sha256 == media.stored_sha256


def test_no_pixel_decode_for_rejected_candidate(tmp_path):
    from unittest.mock import patch
    path = tmp_path / "image.png"
    fixture_png(path)
    with patch("PIL.ImageChops.difference", side_effect=AssertionError("rejected candidate must not be decoded")):
        media = prepare_media(path, tmp_path / "work", min_saving=0.9999)
    assert media.upload_file == path


def test_fast_lossless_rgba_preserves_all_channels(tmp_path):
    path = tmp_path / "image.png"
    image = Image.new("RGBA", (512, 512))
    image.paste((200, 30, 70, 0), (0, 0, 256, 256))
    image.paste((100, 200, 20, 255), (256, 256, 512, 512))
    image.save(path, compress_level=0)
    media = prepare_media(path, tmp_path / "work", min_saving=0)
    assert media.encoding == "webp_lossless"
    with Image.open(media.upload_file) as decoded:
        assert image.tobytes() == decoded.convert("RGBA").tobytes()
