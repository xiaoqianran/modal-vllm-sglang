"""Concurrency contracts for the formal image pipeline; no GPU or network."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import threading
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from orchestration.sdk import model_specs
from runtime.serving.inference_api import ImageGenerationClient
from runtime.serving.model_runtime import ModelRuntime
from runtime.serving.preview_batch import PreviewWriter, SerializedCommitVolume
from runtime.tasks import artifacts
from runtime.tasks.task_identity import intent_digest
from runtime.telemetry.recorder import Recorder
from runtime.utils import files
from tests.test_sdk_orchestration import PNG


PREFIX = "model-results/minimax-omni/test/pipeline"
TIMEOUT = 5


def make_runtime(root):
    runtime = ModelRuntime()
    runtime.spec = model_specs()["minimax-omni"]
    runtime.cache_fingerprint = "test-cache"
    runtime.volume = SerializedCommitVolume(Mock())
    runtime.batch_lock = threading.Lock()
    runtime.output_condition = threading.Condition()
    runtime.active_outputs = set()
    runtime.telemetry = Recorder()
    runtime._ensure_engine = Mock()
    runtime.monitor = object()
    runtime.resources = Mock()
    runtime.resources.snapshot.return_value = {}
    runtime.result_writer = PreviewWriter(runtime.volume, root, "", None, flush_interval=.01)
    return runtime


def run(runtime, item):
    return ModelRuntime.generate_one._get_raw_f()(
        runtime, item, PREFIX, runtime.cache_fingerprint, intent_digest(runtime.spec))


def mounted(root):
    return lambda *parts: root if parts == ("/data",) else Path(*parts)


def item(name):
    return {"id": name, "prompt": name, "seed": 1}


def metadata(root, name):
    return root / PREFIX / (name + ".json")


def test_next_edit_prefetches_and_infers_while_previous_publication_is_pending(tmp_path):
    runtime = make_runtime(tmp_path)
    first_gpu, release_gpu = threading.Event(), threading.Event()
    prefetched, second_gpu = threading.Event(), threading.Event()
    writing, release_write = threading.Event(), threading.Event()
    reference = {"path": "model-results/minimax-omni/prior/source.png",
                 "sha256": hashlib.sha256(PNG).hexdigest(), "size": len(PNG)}
    runtime.volume.volume.read_file.return_value = [PNG]
    read_image, atomic_write = artifacts.read_image, files.atomic_write

    def generate(payload, **kwargs):
        assert payload["prompt"] == "first"
        first_gpu.set()
        assert release_gpu.wait(TIMEOUT)
        return PNG, ".png"

    def prefetch(*args, **kwargs):
        blob = read_image(*args, **kwargs)
        prefetched.set()
        return blob

    def edit(payload, source, **kwargs):
        assert source == PNG
        assert "image_artifact" not in payload
        second_gpu.set()
        return PNG, ".png"

    def write(path, data):
        if path.name == "first.png":
            writing.set()
            assert release_write.wait(TIMEOUT)
        return atomic_write(path, data)

    try:
        with patch("runtime.serving.inference_execution.Path", side_effect=mounted(tmp_path)), \
             patch("runtime.serving.model_runtime.Path", side_effect=mounted(tmp_path)), \
             patch("modal.current_input_id", return_value="in-test"), \
             patch.object(ImageGenerationClient, "generate", side_effect=generate), \
             patch.object(ImageGenerationClient, "edit", side_effect=edit), \
             patch.object(artifacts, "read_image", side_effect=prefetch), \
             patch.object(files, "atomic_write", side_effect=write), \
             ThreadPoolExecutor(max_workers=2) as pool:
            try:
                first = pool.submit(run, runtime, item("first"))
                assert first_gpu.wait(TIMEOUT)
                second = pool.submit(run, runtime, {**item("second"), "operation": "i2i",
                                                    "image_artifact": reference})
                assert prefetched.wait(TIMEOUT)
                assert not second_gpu.is_set()
                assert not metadata(tmp_path, "second").exists()
                release_gpu.set()
                assert writing.wait(TIMEOUT)
                assert second_gpu.wait(TIMEOUT)
                assert not first.done()
                assert files.read_json(metadata(tmp_path, "first"))["status"] == "collecting"
                release_write.set()
                assert first.result(timeout=TIMEOUT)["status"] == "completed"
                assert second.result(timeout=TIMEOUT)["status"] == "completed"
            finally:
                release_gpu.set()
                release_write.set()
    finally:
        runtime.result_writer.close()
    assert runtime.active_outputs == set()


def test_duplicate_output_waits_for_durable_receipt_without_regeneration(tmp_path):
    runtime = make_runtime(tmp_path)
    writing, release_write, duplicate_waiting = (threading.Event() for _ in range(3))
    atomic_write = files.atomic_write
    wait_for = runtime.output_condition.wait_for

    def write(path, data):
        if path.name == "same.png":
            writing.set()
            assert release_write.wait(TIMEOUT)
        return atomic_write(path, data)

    def observe_wait(predicate):
        if not predicate():
            duplicate_waiting.set()
        return wait_for(predicate)

    try:
        with patch("runtime.serving.inference_execution.Path", side_effect=mounted(tmp_path)), \
             patch("runtime.serving.model_runtime.Path", side_effect=mounted(tmp_path)), \
             patch("modal.current_input_id", return_value="in-test"), \
             patch.object(ImageGenerationClient, "generate", return_value=(PNG, ".png")) as generate, \
             patch.object(files, "atomic_write", side_effect=write), \
             patch.object(runtime.output_condition, "wait_for", side_effect=observe_wait), \
             ThreadPoolExecutor(max_workers=2) as pool:
            try:
                first = pool.submit(run, runtime, item("same"))
                assert writing.wait(TIMEOUT)
                duplicate = pool.submit(run, runtime, item("same"))
                assert duplicate_waiting.wait(TIMEOUT)
                assert not duplicate.done()
                assert generate.call_count == 1
                release_write.set()
                a, b = first.result(timeout=TIMEOUT), duplicate.result(timeout=TIMEOUT)
                assert a["status"] == b["status"] == "completed"
                assert a["sha256"] == b["sha256"] == hashlib.sha256(PNG).hexdigest()
                generate.assert_called_once()
                with pytest.raises(ValueError, match="signature changed"):
                    run(runtime, {**item("same"), "prompt": "different"})
                generate.assert_called_once()
            finally:
                release_write.set()
    finally:
        runtime.result_writer.close()
    assert runtime.active_outputs == set()


def test_submission_checkpoint_failure_does_not_call_or_replay_engine(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.volume.volume.commit.side_effect = OSError("commit failed")
    try:
        with patch("runtime.serving.inference_execution.Path", side_effect=mounted(tmp_path)), \
             patch("runtime.serving.model_runtime.Path", side_effect=mounted(tmp_path)), \
             patch("modal.current_input_id", return_value="in-test"), \
             patch.object(ImageGenerationClient, "generate") as generate:
            with pytest.raises(OSError, match="commit failed"):
                run(runtime, item("first"))
            generate.assert_not_called()
            assert runtime.active_outputs == set()
            assert not runtime.batch_lock.locked()
            assert files.read_json(metadata(tmp_path, "first"))["status"] == "submitting"
            runtime.volume.volume.commit.side_effect = None
            with pytest.raises(RuntimeError, match="Uncertain submission"):
                run(runtime, item("first"))
            generate.assert_not_called()
    finally:
        runtime.result_writer.close()


def test_publication_failure_prevents_further_engine_admission(tmp_path):
    runtime = make_runtime(tmp_path)
    first = metadata(tmp_path, "first")

    def commit():
        if first.exists() and files.read_json(first)["status"] == "collecting":
            raise OSError("lost publication commit")

    runtime.volume.volume.commit.side_effect = commit
    try:
        with patch("runtime.serving.inference_execution.Path", side_effect=mounted(tmp_path)), \
             patch("runtime.serving.model_runtime.Path", side_effect=mounted(tmp_path)), \
             patch("modal.current_input_id", return_value="in-test"), \
             patch.object(ImageGenerationClient, "generate", return_value=(PNG, ".png")) as generate:
            with pytest.raises(RuntimeError, match="Publication uncertain"):
                run(runtime, item("first"))
            assert files.read_json(first)["status"] == "collecting"
            assert runtime.active_outputs == set()
            assert not runtime.batch_lock.locked()
            with pytest.raises(RuntimeError, match="Preview writer failed"):
                run(runtime, item("second"))
            assert not metadata(tmp_path, "second").exists()
            generate.assert_called_once()
    finally:
        with pytest.raises(RuntimeError, match="Preview writer failed"):
            runtime.result_writer.close()


def test_reference_failure_never_enters_engine_or_creates_submission(tmp_path):
    runtime = make_runtime(tmp_path)
    reference = {"path": "model-results/minimax-omni/prior/missing.png",
                 "sha256": hashlib.sha256(PNG).hexdigest(), "size": len(PNG)}
    runtime.volume.volume.read_file.side_effect = FileNotFoundError("source missing")
    try:
        with patch("runtime.serving.inference_execution.Path", side_effect=mounted(tmp_path)), \
             patch("runtime.serving.model_runtime.Path", side_effect=mounted(tmp_path)), \
             patch("modal.current_input_id", return_value="in-test"), \
             patch.object(ImageGenerationClient, "edit") as edit:
            with pytest.raises(FileNotFoundError, match="source missing"):
                run(runtime, {**item("edit"), "operation": "i2i", "image_artifact": reference})
            assert not metadata(tmp_path, "edit").exists()
            runtime.volume.volume.commit.assert_not_called()
            runtime._ensure_engine.assert_not_called()
            edit.assert_not_called()
            assert runtime.active_outputs == set()
            assert not runtime.batch_lock.locked()
    finally:
        runtime.result_writer.close()


def test_writer_health_is_rechecked_inside_engine_slot(tmp_path):
    runtime = make_runtime(tmp_path)
    try:
        with patch("runtime.serving.inference_execution.Path", side_effect=mounted(tmp_path)), \
             patch("runtime.serving.model_runtime.Path", side_effect=mounted(tmp_path)), \
             patch("modal.current_input_id", return_value="in-test"), \
             patch.object(runtime.result_writer, "check",
                          side_effect=[None, RuntimeError("writer failed after prefetch")]), \
             patch.object(ImageGenerationClient, "generate") as generate:
            with pytest.raises(RuntimeError, match="writer failed after prefetch"):
                run(runtime, item("first"))
            assert not metadata(tmp_path, "first").exists()
            runtime.volume.volume.commit.assert_not_called()
            runtime._ensure_engine.assert_not_called()
            generate.assert_not_called()
            assert runtime.active_outputs == set()
            assert not runtime.batch_lock.locked()
    finally:
        runtime.result_writer.close()
