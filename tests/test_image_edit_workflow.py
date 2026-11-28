"""Formal H3 edit dependency, binary transport and recovery tests."""
import base64
import hashlib
import json
from pathlib import Path
import shutil
from unittest.mock import Mock, patch

import httpx
import pytest

from orchestration import sdk, state
from orchestration.storage_gc import _refs
from orchestration.workflow_recovery import validate_payload
from pipeline import s01_build_plan as runner
from pipeline.s02_submit_shots import payload_for
from orchestration.protocol import run_id
from runtime.tasks.artifacts import image_reference, image_limit
from runtime.tasks.task_spec import prepare
from runtime.serving.inference_api import ImageGenerationClient
from runtime.serving.inference_execution import execute
from runtime.tasks.task_lifecycle import SubmissionRejected
from runtime.utils.media import MAX_FRAME, MAX_EDIT_IMAGE
from runtime.config.storage import volume_key
from runtime.telemetry.recorder import close_local
from tests.test_sdk_orchestration import FakeBackend, PNG


def plan_for(tmp_path, workflow, item):
    shutil.copytree(runner.ROOT / "workflows", tmp_path / "workflows")
    task = tmp_path / "tasks/099-edit-test.json"
    state.save(task, {"version": 1, "name": "099-edit-test", "workflow": workflow,
                      "delivery": {"provider": "local"}, "items": [item]})
    return runner.build_plan(task, root=tmp_path)


def ref(blob=PNG):
    return {"path": "model-results/minimax/prior/source.png",
            "sha256": hashlib.sha256(blob).hexdigest(), "size": len(blob)}


def test_formal_dag_uses_cloud_reference_not_download(tmp_path):
    plan = plan_for(tmp_path, "minimax-t2i-i2i",
                    {"id": "001", "prompt": "landscape", "edit_prompt": "snow"})
    assert [j["uses"] for j in plan["jobs"]] == ["minimax.t2i", "minimax.i2i"]
    source, edit = plan["jobs"]
    assert edit["inputs"]["image"] == {"job": source["key"], "output": "image"}
    spec = plan["services"]["minimax"]["spec"]
    backend = Mock()
    backend.prepare_edit_original.side_effect = lambda ref: ref
    assert sdk.item_for(tmp_path, edit, {source["key"]: {"status": "queued"}}, spec, backend) is None
    records = {source["key"]: {"status": "completed", "artifacts": {"image": ref()}}}
    item = sdk.item_for(tmp_path, edit, records, spec, backend)
    assert item["image_artifact"] == ref()
    assert prepare(spec, item)[0]["image_artifact"] == ref()
    backend.upload.assert_not_called()


def test_4k_cloud_input_is_valid_for_edit_not_video(tmp_path):
    large = {**ref(), "size": 48 * 1024 * 1024}
    assert image_reference(large, max_bytes=image_limit("image")) == large
    with pytest.raises(ValueError):
        image_reference(large, max_bytes=image_limit("first_frame"))
    with pytest.raises(ValueError):
        image_reference({**large, "size": MAX_EDIT_IMAGE+1}, max_bytes=MAX_EDIT_IMAGE)
    plan = plan_for(tmp_path, "minimax-i2i", {"id": "001", "prompt": "snow", "image": {"artifact": large}})
    directory = runner.new_run(plan, root=tmp_path)
    try:
        backend = FakeBackend()
        backend.files[volume_key("/" + large["path"])] = PNG
        backend.upload = Mock(side_effect=AssertionError("cloud original must not upload"))
        payload = payload_for(directory, plan, "001", backend)
        identity = run_id(payload["plan"], "001")
        validate_payload(payload, identity, plan["volume"])
        roots = _refs({"payload": payload})
        assert roots[large["path"]]["ref"] == large
        backend.upload.assert_not_called()
    finally:
        close_local(tmp_path / ".runtime/logs")


def test_multipart_edit_single_request_and_rejection():
    client = ImageGenerationClient("http://localhost")
    response = Mock(is_error=False)
    response.json.return_value = {"data": [{"b64_json": base64.b64encode(PNG).decode()}]}
    with patch("httpx.Client") as factory:
        request = factory.return_value.__enter__.return_value.post
        request.return_value = response
        assert client.edit({"prompt": "snow"}, PNG) == (PNG, ".png")
        request.assert_called_once_with("http://localhost/v1/images/edits",
                                        data={"prompt": "<Picture 1> snow"},
                                        files={"image": ("source.png", PNG, "image/png")})
        response.is_error = True
        response.headers = {"X-Generation-Rejected": "1"}
        with pytest.raises(SubmissionRejected):
            client.edit({"prompt": "snow"}, PNG)
        assert request.call_count == 2


def test_formal_execution_reads_original_and_publishes_result(tmp_path):
    spec = sdk.model_specs()["minimax"]
    item = {"id": "edit1", "operation": "i2i", "prompt": "snow", "seed": 42,
            "params": {}, "image_artifact": ref()}
    volume = Mock()
    volume.read_file.return_value = [PNG]
    prefix = "model-results/minimax/test/edit"
    real_path = Path
    def mounted(*parts):
        return tmp_path if parts == ("/data",) else real_path(*parts)
    with patch("runtime.serving.inference_execution.Path", side_effect=mounted), \
         patch.object(ImageGenerationClient, "edit", return_value=(PNG, ".png")) as edit, \
         patch.object(ImageGenerationClient, "generate", side_effect=AssertionError("wrong endpoint")):
        result = next(execute(spec, [item], prefix, volume))
        assert result["status"] == "completed"
        assert (tmp_path / prefix / "edit1.png").read_bytes() == PNG
        assert edit.call_args.args[1] == PNG
        assert "image_artifact" not in edit.call_args.args[0]
        volume.read_file.assert_called_once_with(volume_key("/" + ref()["path"]))
        # Recover completed result without another inference.
        assert next(execute(spec, [item], prefix, volume)) == result
        edit.assert_called_once()


def test_edit_requires_cloud_artifact_and_uses_distinct_signature():
    spec = sdk.model_specs()["minimax"]
    item = {"id": "edit", "operation": "i2i", "prompt": "snow", "seed": 1, "params": {}}
    with pytest.raises(ValueError):
        prepare(spec, item)
    with pytest.raises(ValueError, match="cloud artifact"):
        prepare(spec, {**item, "image_base64": base64.b64encode(PNG).decode()})
    _, sig = prepare(spec, {**item, "image_artifact": ref()})
    _, changed = prepare(spec, {**item, "image_artifact": {**ref(), "sha256": "a"*64}})
    assert sig != changed


def test_cpu_hf_restore_only_on_confirmed_volume_miss():
    backend = FakeBackend()
    original = ref()
    restored = {**original, "path": "workflow-inputs/preview-originals/" + original["sha256"]}
    with patch("runtime.serving.preview_batch.prepare_original", return_value={
            "source": "hf", "artifact": restored}) as restore:
        assert backend.prepare_edit_original(original) == restored
        restore.assert_called_once_with(backend.volume, None, original)
        backend.files[volume_key("/" + original["path"])] = PNG
        assert backend.prepare_edit_original(original) == original
        restore.assert_called_once()
        backend.volume.read_file.side_effect = PermissionError("unavailable")
        with pytest.raises(PermissionError):
            backend.prepare_edit_original(original)
        restore.assert_called_once()


def test_native_file_transport_does_not_decode_base64_or_repeat(tmp_path):
    directory = tmp_path / "engine"
    directory.mkdir()
    path = directory / "output.png"
    path.write_bytes(PNG)
    client = ImageGenerationClient("http://localhost", output_directory=directory)
    reply = {"data": [{"file_path": str(path), "url": "/v1/images/id/content"}]}
    with patch("runtime.serving.inference_api.request_json", return_value=reply) as request:
        assert client.generate({"prompt": "snow", "response_format": "b64_json"}) == (PNG, ".png")
        assert request.call_args.args[1]["response_format"] == "url"
        assert not path.exists()
        request.assert_called_once()
    outside = tmp_path / "other.png"
    outside.write_bytes(PNG)
    with patch("runtime.serving.inference_api.request_json", return_value={"data": [{"file_path": str(outside)}]}) as request:
        with pytest.raises(ValueError, match="escaped"):
            client.generate({"prompt": "snow"})
        assert outside.exists()
        request.assert_called_once()


def test_native_multipart_transport_uses_file_receipt(tmp_path):
    path = tmp_path / "output.png"
    path.write_bytes(PNG)
    response = Mock(is_error=False)
    response.json.return_value = {"data": [{"file_path": str(path)}]}
    with patch("httpx.Client") as factory:
        request = factory.return_value.__enter__.return_value.post
        request.return_value = response
        assert ImageGenerationClient("http://localhost", output_directory=tmp_path).edit({"prompt": "snow"}, PNG) == (PNG, ".png")
        assert request.call_args.kwargs["data"]["response_format"] == "url"
        assert not path.exists()
        request.assert_called_once()


def test_formal_writer_commits_before_completed_receipt(tmp_path):
    from runtime.serving.preview_batch import PreviewWriter
    import threading
    spec = sdk.model_specs()["minimax"]
    volume = Mock()
    observed = []
    prefix = "model-results/minimax/test/async"
    metadata = tmp_path / prefix / "first.json"
    volume.commit.side_effect = lambda: observed.append(json.loads(metadata.read_text())["status"])
    writer = PreviewWriter(volume, tmp_path, "", None, flush_interval=.01)
    item = {"id": "first", "prompt": "snow", "seed": 1}
    try:
        with patch("runtime.serving.inference_execution.Path", side_effect=lambda *p: tmp_path if p == ("/data",) else Path(*p)), \
             patch.object(ImageGenerationClient, "generate", return_value=(PNG, ".png")):
            future = next(execute(spec, [item], prefix, volume, result_writer=writer))
            result = future.result(timeout=5)
            assert result["status"] == "completed"
            assert observed == ["submitting", "collecting", "completed"]
            assert (tmp_path / prefix / "first.png").read_bytes() == PNG
    finally:
        writer.close()


def test_formal_writer_failure_releases_future_without_completed_receipt(tmp_path):
    from runtime.serving.preview_batch import PreviewWriter
    volume = Mock()
    volume.commit.side_effect = OSError("lost commit acknowledgement")
    writer = PreviewWriter(volume, tmp_path, "", None, flush_interval=.01)
    finish = Mock()
    try:
        future = writer.enqueue_publication(PNG, lambda data: ({"status": "collecting"}, finish))
        with pytest.raises(RuntimeError, match="Publication uncertain"):
            future.result(timeout=5)
        finish.assert_not_called()
    finally:
        with pytest.raises(RuntimeError):
            writer.close()


def test_dependency_wait_wakes_on_existing_call_without_sleep():
    from pipeline.s03_run_shot import wait_for_progress
    backend = Mock()
    plan = {"services": {"minimax": {}}, "jobs": [{"uses": "minimax.i2i"}]}
    records = {"source": {"status": "queued", "call_id": "fc-1"}, "edit": {"status": "blocked"}}
    with patch("pipeline.s03_run_shot.time.sleep") as sleep:
        wait_for_progress(plan, records, backend, 2)
        backend.call_handle.assert_called_once_with("fc-1")
        backend.call_handle.return_value.get.assert_called_once_with(timeout=2)
        sleep.assert_not_called()


def test_local_fallback_only_after_cloud_miss(tmp_path):
    from orchestration.sdk import resolve_edit_binding, EditOriginalMissing
    backend = Mock()
    original = ref()
    fallback = {"path": ".runner/inputs/" + original["sha256"], "sha256": original["sha256"], "size": original["size"]}
    binding = {**original, "storage": "volume", "fallback": fallback}
    backend.prepare_edit_original.return_value = original
    assert resolve_edit_binding(tmp_path, binding, backend) == original
    backend.upload.assert_not_called()
    backend.prepare_edit_original.side_effect = PermissionError()
    with pytest.raises(PermissionError):
        resolve_edit_binding(tmp_path, binding, backend)
    backend.upload.assert_not_called()
    backend.prepare_edit_original.side_effect = EditOriginalMissing()
    assert resolve_edit_binding(tmp_path, binding, backend) == backend.upload.return_value
    backend.upload.assert_called_once_with(tmp_path, fallback, managed=False)


def test_plan_freezes_matching_local_fallback(tmp_path):
    original = ref()
    (tmp_path / "original.png").write_bytes(PNG)
    plan = plan_for(tmp_path, "minimax-i2i", {"id": "001", "prompt": "snow",
                    "image": {"artifact": original, "file": "original.png"}})
    binding = plan["jobs"][0]["inputs"]["image"]
    assert binding["fallback"]["sha256"] == original["sha256"]
    assert binding["fallback"]["path"] in plan["imports"]
