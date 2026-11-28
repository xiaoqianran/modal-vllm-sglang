"""Offline verification for the H3 cloud-only preview path."""
import hashlib
import io
from contextlib import contextmanager
from urllib.error import HTTPError
from pathlib import Path
from unittest.mock import Mock
import pytest
from PIL import Image
from runtime.config.storage import volume_key
from runtime.serving.preview_batch import (
    cache_reference, prepare_original, read_original, run_preview_batch, stage_local
)


def png(color="red"):
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(buffer, format="PNG")
    return buffer.getvalue()


def ref(blob, path="model-results/minimax/old/source.png"):
    return {"path": path, "size": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}


class Volume:
    def __init__(self):
        self.files, self.reads, self.uploads = {}, [], []
        self.commits = 0

    def read_file(self, key):
        self.reads.append(key)
        if key not in self.files:
            raise FileNotFoundError(key)
        yield self.files[key]

    @contextmanager
    def batch_upload(self, force=False):
        assert force is False
        def put(stream, key):
            if key in self.files:
                raise FileExistsError(key)
            self.files[key] = stream.read()
            self.uploads.append(key)
        yield type("Batch", (), {"put_file": staticmethod(put)})()

    def commit(self):
        self.commits += 1


class Response(io.BytesIO):
    status = 200


def test_modal_mount_precedes_hf_and_sdk(tmp_path):
    blob = png()
    asset = ref(blob)
    path = tmp_path / asset["path"]
    path.parent.mkdir(parents=True)
    path.write_bytes(blob)
    vol, opener = Volume(), Mock(side_effect=AssertionError("HF touched"))
    result = prepare_original(vol, tmp_path, {**asset, "hf": {"bucket": "a/b", "path": "x.png"}}, opener=opener)
    assert result == {"source": "modal", "artifact": asset}
    assert vol.reads == []
    opener.assert_not_called()


def test_sdk_committed_data_precedes_hf(tmp_path):
    blob = png()
    asset = ref(blob)
    vol, opener = Volume(), Mock(side_effect=AssertionError("HF touched"))
    vol.files[volume_key("/" + asset["path"])] = blob
    assert prepare_original(vol, tmp_path, asset, opener=opener)["source"] == "modal"
    opener.assert_not_called()


def test_hf_restore_caches_and_reuses(tmp_path):
    blob = png()
    asset = {**ref(blob), "hf": {"bucket": "a/b", "path": "original.png"}}
    vol, opener = Volume(), Mock(side_effect=lambda *a, **kw: Response(blob))
    result = prepare_original(vol, tmp_path, asset, opener=opener)
    assert result["source"] == "hf"
    assert result["artifact"] == cache_reference(asset)
    assert read_original(vol, tmp_path, result["artifact"]) == blob
    assert prepare_original(vol, tmp_path, asset, opener=opener)["source"] == "modal"
    assert len(vol.uploads) == 1
    opener.assert_called_once()


def test_only_cloud_miss_allows_local(tmp_path):
    blob = png()
    asset = {**ref(blob), "hf": {"bucket": "a/b", "path": "original.png"}}
    vol = Volume()
    opener = Mock(side_effect=HTTPError("https://huggingface.co", 404, "", None, None))
    assert prepare_original(vol, tmp_path, asset, opener=opener)["source"] == "missing"
    local = tmp_path / "source.png"
    local.write_bytes(blob)
    staged = stage_local(vol, asset, local)
    assert read_original(vol, tmp_path, staged) == blob
    assert prepare_original(vol, tmp_path, asset, opener=opener)["source"] == "modal"
    assert len(vol.uploads) == 1


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_hf_errors_do_not_trigger_local_fallback(tmp_path, status):
    asset = {**ref(png()), "hf": {"bucket": "a/b", "path": "original.png"}}
    vol = Volume()
    with pytest.raises(HTTPError):
        prepare_original(vol, tmp_path, asset, opener=Mock(side_effect=HTTPError("https://huggingface.co", status, "", None, None)))
    assert not vol.uploads


def test_hf_corruption_is_not_published(tmp_path):
    blob = png()
    asset = {**ref(blob), "hf": {"bucket": "a/b", "path": "original.png"}}
    vol = Volume()
    with pytest.raises(ValueError, match="mismatch"):
        prepare_original(vol, tmp_path, asset, opener=lambda *a, **kw: Response(png("blue")))
    assert not vol.uploads


def test_48_mib_original_not_limited_by_video_frame_cap(tmp_path):
    blob = b"\x89PNG\r\n\x1a\n" + bytes(48 * 1024 * 1024 - 8)
    asset = ref(blob)
    vol = Volume()
    vol.files[volume_key("/" + asset["path"])] = blob
    assert prepare_original(vol, tmp_path, asset)["source"] == "modal"


def pairs():
    return [{"group": "A", "case": str(i), "size": "32x32", "area": 1024,
             "prompt": "generate", "edit": "edit"} for i in range(3)]


def test_batch_keeps_original_in_memory_and_returns_references(tmp_path):
    a, b = png(), png("blue")
    calls = []
    vol = Volume()
    def render(prompt, source, size, area):
        calls.append((prompt, source))
        if prompt == "edit":
            assert source is a
            return b
        assert source is None
        return a
    report = run_preview_batch(vol, tmp_path, "study", pairs(), render, container_id="same")
    assert report["status"] == "completed"
    assert len(report["records"]) == 6
    assert calls == [("generate", None), ("edit", a)] * 3
    assert all(row["container_id"] == "same" for row in report["records"])
    assert not vol.reads and not vol.uploads
    for row in report["records"]:
        assert (tmp_path / row["artifact"]["path"]).is_file()
    assert report["records"][1]["source"] == report["records"][0]["artifact"]
    assert 3 <= vol.commits < 8
    assert report['writer']['commits'] == vol.commits - 2
    with pytest.raises(FileExistsError):
        run_preview_batch(vol, tmp_path, "study", pairs(), render)


def test_existing_source_no_generation(tmp_path):
    a, b = png(), png("blue")
    asset = ref(a)
    vol = Volume()
    vol.files[volume_key("/" + asset["path"])] = a
    pair = {**pairs()[0], "source": asset}
    render = Mock(return_value=b)
    report = run_preview_batch(vol, tmp_path, "reuse", [pair], render)
    render.assert_called_once_with("edit", a, "32x32", 1024)
    assert report["records"][0]["source"] == asset


def test_failed_edit_never_replays_and_keeps_original(tmp_path):
    a = png()
    vol = Volume()
    render = Mock(side_effect=[a, TimeoutError("unknown acceptance")])
    report = run_preview_batch(vol, tmp_path, "failed", pairs(), render)
    assert report["status"] == "incomplete"
    assert render.call_count == 2
    assert (tmp_path / report["records"][0]["artifact"]["path"]).read_bytes() == a
    assert (tmp_path / "model-results/minimax/failed/results.json").exists()


def test_duplicate_rejected_before_gpu(tmp_path):
    render = Mock()
    with pytest.raises(ValueError, match="Duplicate"):
        run_preview_batch(Volume(), tmp_path, "study", [pairs()[0]] * 2, render)
    render.assert_not_called()



def test_default_hf_content_address(tmp_path):
    blob = png()
    asset = ref(blob)
    opener = Mock(side_effect=lambda *a, **kw: Response(blob))
    result = prepare_original(Volume(), tmp_path, asset, opener=opener)
    assert result["source"] == "hf"
    url = opener.call_args.args[0].full_url
    assert url.endswith(f"/assets/sha256/{asset['sha256'][:2]}/{asset['sha256']}.png")


def test_hf_lossless_different_bytes_are_not_original(tmp_path):
    blob = png()
    asset = {**ref(blob), "hf": {"bucket": "a/b", "path": "original.webp",
                                  "sha256": "a"*64}}
    opener = Mock()
    with pytest.raises(ValueError, match="byte identity"):
        prepare_original(Volume(), tmp_path, asset, opener=opener)
    opener.assert_not_called()


def test_writer_overlaps_inference_and_commit(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    blob = png()
    entered, release, second_render = threading.Event(), threading.Event(), threading.Event()
    vol = Volume()
    original = vol.commit
    def commit():
        original()
        if vol.commits == 2:
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test did not release commit")
    vol.commit = commit
    def render(prompt, source, size, area):
        if prompt == "edit":
            assert entered.wait(5)
            second_render.set()
        return blob
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_preview_batch, vol, tmp_path, "overlap", pairs()[:1], render,
                             writer_options={"batch_files": 1})
        try:
            assert second_render.wait(5), "edit must start before commit completes"
        finally:
            release.set()
        result = future.result(timeout=5)
    assert result["status"] == "completed"
    assert len(result["records"]) == 2


def test_writer_batches_and_bounds_pending(tmp_path):
    blob = png()
    vol = Volume()
    report = run_preview_batch(vol, tmp_path, "bounded", pairs(),
                               Mock(return_value=blob),
                               writer_options={"batch_files": 2, "max_pending": 2})
    stats = report["writer"]
    assert stats["peak_pending"] <= 2
    assert stats["peak_pending_bytes"] <= 2 * len(blob)
    assert stats["commits"] == 3
    assert vol.commits == 5
    assert len(report["records"]) == 6


def test_writer_commit_failure_stops_without_false_summary(tmp_path):
    vol = Volume()
    original = vol.commit
    def commit():
        original()
        if vol.commits == 2:
            raise OSError("lost commit acknowledgement")
    vol.commit = commit
    with pytest.raises(RuntimeError, match="publication uncertain"):
        run_preview_batch(vol, tmp_path, "uncertain", pairs(), Mock(return_value=png()),
                          writer_options={"batch_files": 1, "max_pending": 1})
    assert vol.commits == 2
    assert not (tmp_path / "model-results/minimax/uncertain/results.json").exists()


def test_writer_invalid_image_stops_without_false_summary(tmp_path):
    vol = Volume()
    with pytest.raises(RuntimeError, match="publication uncertain"):
        run_preview_batch(vol, tmp_path, "invalid", pairs(),
                          Mock(return_value=b"\x89PNG\r\n\x1a\n" + b"bad"),
                          writer_options={"batch_files": 1})
    assert vol.commits == 1
    assert not (tmp_path / "model-results/minimax/invalid/results.json").exists()


def test_writer_flushes_partial_batch_after_render_error(tmp_path):
    vol = Volume()
    render = Mock(side_effect=[png(), TimeoutError("uncertain generation")])
    report = run_preview_batch(vol, tmp_path, "partial", pairs(), render)
    assert report["status"] == "incomplete"
    assert report["writer"]["commits"] == 1
    assert report["records"][0]["status"] == "completed"
    assert report["records"][1]["status"] == "error"


def test_v3_delivery_protocol_and_overlap(tmp_path):
    import threading
    from runtime.serving.preview_delivery import preview_intents, DeliveryWake
    from runtime.delivery import outbox
    from runtime.tasks.job_client import check_record
    batch = pairs()[:1]
    intents = preview_intents("delivery", batch)
    vol = Volume()
    started, release = threading.Event(), threading.Event()
    seen = []
    class Mounted:
        def read_file(self, key):
            from runtime.config.storage import VOLUME_SUB_PATH
            yield (tmp_path / key.removeprefix(VOLUME_SUB_PATH).lstrip("/")).read_bytes()
    def spawn(ids):
        for identity in ids:
            intent = next(x for x in intents.values() if x["id"] == identity)
            outbox.validate_intent(intent, identity)
            result = outbox.resolve(Mounted(), intent)
            assert result["artifact"]["sha256"] == ref(png())["sha256"]
            seen.append(identity)
        started.set()
        release.wait(5)
    wake = DeliveryWake(spawn)
    def render(prompt, source, size, area):
        if prompt == "edit":
            assert started.wait(5), "CPU dispatch must precede end of inference"
        return png()
    try:
        result = run_preview_batch(vol, tmp_path, "delivery", batch, render,
                                   writer_options={"batch_files": 1},
                                   intents=intents, on_commit=wake.submit)
        assert not release.is_set()
        assert result["status"] == "completed"
        assert len(result["records"]) == 2
        for row in result["records"]:
            intent = intents[(row["group"], row["case"], row["task"])]
            check_record(intent["locator"], row)
            assert outbox.resolve(Mounted(), intent)["artifact"]["path"] == row["artifact"]["path"]
    finally:
        release.set()
        wake.close()
    assert len(seen) == 2


def test_dispatch_failure_does_not_fail_generation(tmp_path):
    from runtime.serving.preview_delivery import preview_intents
    batch = pairs()[:1]
    intents = preview_intents("deferred", batch)
    notify = Mock(side_effect=TimeoutError("dispatch unavailable"))
    result = run_preview_batch(Volume(), tmp_path, "deferred", batch, Mock(return_value=png()),
                               intents=intents, on_commit=notify,
                               writer_options={"batch_files": 1})
    assert result["status"] == "completed"
    assert notify.call_count == 2
    assert all(r["delivery_id"] for r in result["records"])


def test_uncommitted_batch_never_dispatches(tmp_path):
    from runtime.serving.preview_delivery import preview_intents
    batch = pairs()[:1]
    vol = Volume()
    original = vol.commit
    def commit():
        original()
        if vol.commits == 2:
            raise OSError("ambiguous commit")
    vol.commit = commit
    notify = Mock()
    with pytest.raises(RuntimeError, match="publication uncertain"):
        run_preview_batch(vol, tmp_path, "uncommitted", batch, Mock(return_value=png()),
                          intents=preview_intents("uncommitted", batch), on_commit=notify,
                          writer_options={"batch_files": 1})
    notify.assert_not_called()


def test_incomplete_batch_marks_unproduced_intents_terminal(tmp_path):
    from runtime.serving.preview_delivery import preview_intents
    from runtime.delivery import outbox
    from runtime.config.storage import VOLUME_SUB_PATH
    batch = pairs()
    intents = preview_intents("partial-delivery", batch)
    result = run_preview_batch(Volume(), tmp_path, "partial-delivery", batch,
                               Mock(side_effect=[png(), TimeoutError()]), intents=intents)
    class Mounted:
        def read_file(self, key):
            yield (tmp_path / key.removeprefix(VOLUME_SUB_PATH).lstrip("/")).read_bytes()
    assert result["status"] == "incomplete"
    assert outbox.resolve(Mounted(), intents[("A","0","source")])["artifact"]
    for key, intent in intents.items():
        if key != ("A","0","source"):
            assert outbox.resolve(Mounted(), intent) == {"terminal": True}


def test_reused_original_not_rearchived(tmp_path):
    from runtime.serving.preview_delivery import preview_intents
    batch = [{**pairs()[0], "source": ref(png())}]
    intents = preview_intents("reuse", batch)
    assert list(intents) == [("A", "0", "edit")]
    assert intents == preview_intents("reuse", batch)
