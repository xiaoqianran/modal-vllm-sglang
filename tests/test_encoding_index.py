"""Encoding index is optional, but media integrity is mandatory."""
import json
from io import BytesIO
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import pytest
from PIL import Image

from runtime.archive.encoding_index import lookup_encoding, mapping_path, mapping_record
from runtime.archive.operations import prepare_media
from runtime.archive.pipeline import asset_path
from runtime.delivery.cloud_cpu import archive_mounted_batch
from runtime.utils.files import sha256_file


class Response(BytesIO):
    status = 200


def test_index_policy_and_full_digest(tmp_path):
    source = tmp_path / "image.png"
    Image.new("RGB", (128, 128), "red").save(source, compress_level=0)
    original = prepare_media(source, tmp_path / "original", "none")
    encoded = prepare_media(source, tmp_path / "encoded")
    assert encoded.encoding == "webp_lossless"
    assert mapping_path(original, 0.05) != mapping_path(original, 0.1)
    provider = Mock(bucket="example/assets")
    payload = json.dumps(mapping_record(encoded)).encode()
    with patch("runtime.archive.encoding_index.urlopen", return_value=Response(payload)):
        hit = lookup_encoding(provider, original, 0.05)
    assert hit.remote_only and hit.stored_sha256 == encoded.stored_sha256
    assert asset_path(hit) == asset_path(encoded)
    provider.verify_digest.assert_called_once_with(asset_path(encoded), encoded.stored_size, encoded.stored_sha256)


@pytest.mark.parametrize("case", ["missing", "bad_json", "wrong_original", "stale", "corrupt"])
def test_index_failure_behavior(tmp_path, case):
    source = tmp_path / "image.png"
    Image.new("RGB", (128, 128), "red").save(source, compress_level=0)
    original = prepare_media(source, tmp_path / "original", "none")
    encoded = prepare_media(source, tmp_path / "encoded")
    provider = Mock(bucket="example/assets")
    missing = HTTPError("https://example.invalid", 404, "missing", {}, None)
    row = mapping_record(encoded)
    if case == "wrong_original":
        row["original_sha256"] = "f" * 64
    payload = b"not json" if case == "bad_json" else json.dumps(row).encode()
    if case == "stale":
        provider.verify_digest.side_effect = missing
    if case == "corrupt":
        provider.verify_digest.side_effect = RuntimeError("SHA256 mismatch")
    with patch("runtime.archive.encoding_index.urlopen",
               side_effect=missing if case == "missing" else None,
               return_value=Response(payload)):
        if case == "corrupt":
            with pytest.raises(RuntimeError, match="SHA256"):
                lookup_encoding(provider, original, 0.05)
        else:
            assert lookup_encoding(provider, original, 0.05) is None
    if case in {"missing", "bad_json", "wrong_original"}:
        provider.verify_digest.assert_not_called()


def test_cross_batch_index_skips_encoding_and_upload(tmp_path):
    from runtime.archive.huggingface import public_url
    source = tmp_path / "model-results" / "image.png"
    source.parent.mkdir()
    Image.new("RGB", (128, 128), "red").save(source, compress_level=0)
    artifact = {"storage": "volume", "type": "image", "path": "model-results/image.png",
                "size": source.stat().st_size, "sha256": sha256_file(source)}
    bucket = "example/assets"
    request = {"artifact": artifact, "project": "one",
               "delivery": {"provider": "huggingface", "bucket": bucket}}
    index = {}
    def publish(entries, *, metrics, indexes):
        for records in indexes.values():
            index.update({path: json.loads(data) for path, data in records.items()})
        return {path: (public_url(bucket, path), "sha256", False) for path in entries}
    def read_index(req, **kwargs):
        path = req.full_url.split("/resolve/", 1)[1]
        if path not in index:
            raise HTTPError(req.full_url, 404, "missing", {}, None)
        return Response(json.dumps(index[path]).encode())
    with patch("runtime.archive.huggingface.HuggingFaceBucket") as factory, \
         patch("runtime.archive.encoding_index.urlopen", side_effect=read_index):
        provider = factory.return_value
        provider.bucket = bucket
        provider.publish_many.side_effect = publish
        first = archive_mounted_batch(tmp_path, [request])[0]["receipt"]
        assert len(index) == 1
        metrics = {}
        with patch("PIL.Image.open", side_effect=AssertionError("unexpected encoding")):
            second = archive_mounted_batch(tmp_path, [dict(request, project="two")], metrics=metrics)[0]["receipt"]
        assert first["path"] == second["path"]
        assert second["reused"] and metrics["encoding_hits"] == 1 and metrics["upload_calls"] == 0
        provider.publish_many.assert_called_once()
        provider.verify_digest.assert_called_once()
        # Same-size source corruption is rejected even when the index exists.
        source.write_bytes(source.read_bytes()[:-1] + b"!")
        assert "error" in archive_mounted_batch(tmp_path, [request])[0]
        provider.verify_digest.assert_called_once()
