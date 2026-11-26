"""Modal CPU archive contract: no PC2 byte relay, GPUs, or external network."""
import hashlib
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from runtime.delivery.cloud_cpu import archive_mounted_artifact
from runtime.delivery.huggingface import archive_from_volume
from runtime.utils.files import sha256_file


def fixture(tmp_path):
    raw = b"\x89PNG\r\n\x1a\n" + b"image payload" * 8
    file = tmp_path / "model-results" / "moss" / "shot.png"
    file.parent.mkdir(parents=True)
    file.write_bytes(raw)
    artifact = {
        "storage": "volume", "type": "image",
        "path": "model-results/moss/shot.png", "size": file.stat().st_size,
        "sha256": sha256_file(file)
    }
    settings = {"provider": "huggingface", "bucket": "seachen/modal-vllm-sglang-assets",
                "compression": "none", "verify": "sha256", "min_saving": 0.05}
    return file, artifact, settings


def test_remote_cpu_reads_mount_not_pc2(tmp_path):
    file, artifact, settings = fixture(tmp_path)
    with patch("runtime.delivery.cloud_cpu.ArchivePipeline") as factory:
        factory.return_value.run.return_value = {"status": "archived", "url": "https://example.invalid"}
        result = archive_mounted_artifact(tmp_path, artifact, settings, "001-project")
    assert result["source_artifact"] == artifact
    factory.return_value.run.assert_called_once_with(file.resolve(), upload=True, expected_sha256=artifact["sha256"])
    assert factory.call_args.args[0].verify == "sha256"


@pytest.mark.parametrize("badpath", ["../outside.png", "model-results/../secrets", "/etc/passwd",
                                     "model-results//x.png", "model-results/foo\\bar.png",
                                     "weights/model.bin"])
def test_remote_cpu_rejects_paths(tmp_path, badpath):
    _, artifact, settings = fixture(tmp_path)
    artifact["path"] = badpath
    with pytest.raises(ValueError):
        archive_mounted_artifact(tmp_path, artifact, settings, "001-project")


def test_remote_cpu_rejects_tampered_source(tmp_path):
    file, artifact, settings = fixture(tmp_path)
    file.write_bytes(b"tampered" + file.read_bytes())
    with patch("runtime.delivery.cloud_cpu.ArchivePipeline") as factory:
        with pytest.raises(ValueError, match="checksum"):
            archive_mounted_artifact(tmp_path, artifact, settings, "001-project")
    factory.assert_not_called()


def test_cpu_function_registered_without_gpu():
    from pipeline import deploy_assets
    assert deploy_assets.archive_asset is not None
    assert deploy_assets.check_bucket is not None


def test_client_only_sends_metadata_without_volume_read():
    _, artifact, settings = fixture(Path(__import__('tempfile').mkdtemp()))
    modal = Mock()
    modal.Function.from_name.return_value.remote.return_value = {"status": "archived"}
    data = archive_from_volume(modal, {"uses": "moss.t2i"},
                               {"artifacts": {"image": artifact}},
                               {"delivery": settings, "project": "001-project"})
    assert data["status"] == "archived"
    modal.Function.from_name.return_value.remote.assert_called_once_with(
        artifact, settings, "001-project")


def test_cloud_archive_image_installs_dotenv_for_delivery_config_import():
    from pathlib import Path
    code = (Path(__file__).resolve().parents[1] / "pipeline/deploy_assets.py").read_text(encoding="utf-8")
    assert '"python-dotenv>=1"' in code


def test_cpu_archiver_uses_official_pypi():
    import ast
    path = Path(__file__).resolve().parents[1] / "pipeline/deploy_assets.py"
    calls = [node for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr == "uv_pip_install"]
    assert len(calls) == 1
    keywords = {kw.arg: ast.literal_eval(kw.value) for kw in calls[0].keywords}
    assert keywords["index_url"] == "https://pypi.org/simple"


def test_remote_cpu_rejects_same_size_corruption(tmp_path):
    file, artifact, settings = fixture(tmp_path)
    payload = file.read_bytes()
    file.write_bytes(payload[:-1] + bytes([payload[-1] ^ 1]))
    with patch("runtime.archive.pipeline.HuggingFaceBucket") as provider:
        with pytest.raises(ValueError, match="checksum"):
            archive_mounted_artifact(tmp_path, artifact, settings, "001-project")
    provider.assert_not_called()


def test_archive_source_version_guard(monkeypatch):
    from runtime.archive.config import archive_source_version, assert_archive_version
    version = archive_source_version()
    assert len(version) == 64
    monkeypatch.setenv("ARCHIVE_DEPLOY_VERSION", version)
    assert assert_archive_version() == version
    monkeypatch.setenv("ARCHIVE_DEPLOY_VERSION", "old")
    with pytest.raises(RuntimeError, match="version mismatch"):
        assert_archive_version()


def test_batch_dedup_distinct_paths_and_corruption(tmp_path):
    from runtime.delivery.cloud_cpu import archive_mounted_batch
    from runtime.archive.huggingface import public_url
    from runtime.archive.operations import prepare_media
    file, artifact, settings = fixture(tmp_path)
    other = file.with_name("other.png")
    other.write_bytes(file.read_bytes())
    bad = file.with_name("bad.png")
    bad.write_bytes(file.read_bytes()[:-1] + b"!")
    requests = [{"artifact": dict(artifact, path=f"model-results/moss/{name}"),
                 "delivery": settings, "project": project}
                for name, project in [("shot.png", "first"), ("other.png", "second"), ("bad.png", "third")]]
    def publish(entries, *, metrics=None):
        assert len(entries) == 1
        return {path: (public_url(settings["bucket"], path), "sha256", False) for path in entries}
    with patch("runtime.archive.huggingface.HuggingFaceBucket") as provider, \
         patch("runtime.archive.operations.prepare_media", wraps=prepare_media) as prepare:
        provider.return_value.publish_many.side_effect = publish
        result = archive_mounted_batch(tmp_path, requests)
    assert prepare.call_count == 1
    first, second = result[0]["receipt"], result[1]["receipt"]
    assert first["path"] == second["path"]
    assert first["source_artifact"] == requests[0]["artifact"]
    assert second["source_artifact"] == requests[1]["artifact"]
    assert first["reused"] is False and second["reused"] is True
    assert "error" in result[2]
    provider.return_value.publish_many.assert_called_once()


def test_batcher_merges_concurrent_requests_and_separates_errors():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from runtime.delivery.huggingface import ArchiveBatcher
    from runtime.delivery.config import normalize_delivery
    modal = Mock()
    batcher = ArchiveBatcher(modal, wait=0.2)
    settings = normalize_delivery({"provider": "huggingface"})
    artifact = {"storage": "volume", "type": "image", "path": "model-results/a.png",
                "size": 10, "sha256": "a"*64}
    requests = [{"artifact": dict(artifact, path=f"model-results/{name}.png"), "delivery": settings, "project": str(i)}
                for i, name in enumerate(["a", "a", "b", "c"])]
    def call(items, version):
        assert len(items) == 3
        return {"archive_version": version, "results": [
            {"error": "test failure"} if item["artifact"]["path"].endswith("/c.png") else
            {"receipt": {"status": "archived", "verification": "sha256", "source_artifact": item["artifact"],
                         "bucket": settings["bucket"], "path": item["artifact"]["path"]}} for item in items]}
    modal.Function.from_name.return_value.remote.side_effect = call
    barrier = Barrier(4)
    def one(request):
        barrier.wait()
        try:
            return batcher.archive(request)
        except RuntimeError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(one, requests))
    assert result[0] == result[1]
    assert result[2]["path"].endswith("/b.png")
    assert result[3] == "test failure"
    modal.Function.from_name.return_value.remote.assert_called_once()
    assert batcher.inflight == {} and batcher.pending == {}


def test_batch_limits(tmp_path):
    from runtime.delivery.cloud_cpu import archive_mounted_batch
    from runtime.archive.config import ARCHIVE_BATCH_BYTES
    _, artifact, settings = fixture(tmp_path)
    request = {"artifact": artifact, "delivery": settings, "project": "a"}
    with pytest.raises(ValueError, match="count"):
        archive_mounted_batch(tmp_path, [request]*5)
    large = dict(request, artifact=dict(artifact, size=ARCHIVE_BATCH_BYTES))
    with pytest.raises(ValueError, match="byte limit"):
        archive_mounted_batch(tmp_path, [large, request])
