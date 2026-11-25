"""SDK request reuse, immutable input staging and read-only dependency audit."""
import base64
import concurrent.futures
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from orchestration import sdk
from orchestration.input_audit import audit
from orchestration.input_store import stage_input
from orchestration.sdk_session import SDKSession
from pipeline.s02_submit_shots import Backend as WorkflowBackend
from runtime.config.storage import VOLUME_SUB_PATH, volume_key
from runtime.tasks.artifacts import resolve_frames
from runtime.tasks.task_identity import intent_digest

PNG = b"\x89PNG\r\n\x1a\n" + b"content" * 8


class Volume:
    object_id = "vo-test"

    def __init__(self):
        self.files = {}
        self.reads = []
        self.uploads = []
        self.lists = []
        self.lock = threading.Lock()

    def read_file(self, key):
        self.reads.append(key)
        if key not in self.files:
            raise FileNotFoundError(key)
        yield self.files[key]

    @contextmanager
    def batch_upload(self, force=False):
        assert not force
        writes = {}
        def put_file(stream, key):
            writes[key] = stream.read()
        yield SimpleNamespace(put_file=put_file)
        with self.lock:
            if set(writes).intersection(self.files):
                raise FileExistsError("conflict")
            self.files.update(writes)
            self.uploads.extend(writes)

    def iterdir(self, path, recursive=False):
        self.lists.append((path, recursive))
        for key, data in self.files.items():
            if key.lstrip("/").startswith(path.rstrip("/") + "/"):
                yield SimpleNamespace(path=key.lstrip("/"), type=1, size=len(data), mtime=100)


@pytest.fixture
def artifact(tmp_path):
    path = tmp_path / "frame.png"
    path.write_bytes(PNG)
    return {"path": path.name, "sha256": hashlib.sha256(PNG).hexdigest(), "size": len(PNG)}


def key(artifact):
    return volume_key("/workflow-inputs/" + artifact["sha256"])


def test_concurrent_identical_frames_stage_once(tmp_path, artifact):
    vol, session = Volume(), SDKSession()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        refs = list(pool.map(lambda _: stage_input(vol, session, tmp_path, artifact), range(12)))
    assert all(ref == refs[0] for ref in refs)
    assert vol.uploads == [key(artifact)]
    assert vol.reads == [key(artifact)]
    assert vol.files[key(artifact)] == PNG
    # A new process verifies existing bytes and never overwrites.
    assert stage_input(vol, SDKSession(), tmp_path, artifact) == refs[0]
    assert vol.uploads == [key(artifact)]
    assert len(vol.reads) == 2


def test_cached_ref_does_not_hide_modified_local_file(tmp_path, artifact):
    vol, session = Volume(), SDKSession()
    stage_input(vol, session, tmp_path, artifact)
    (tmp_path / artifact["path"]).write_bytes(PNG[:-1] + b"x")
    with pytest.raises(ValueError, match="Frozen input"):
        stage_input(vol, session, tmp_path, artifact)
    assert len(vol.uploads) == 1


def test_corrupt_existing_cas_never_overwritten(tmp_path, artifact):
    vol = Volume()
    vol.files[key(artifact)] = PNG[:-1] + b"x"
    with pytest.raises(ValueError, match="content mismatch"):
        stage_input(vol, SDKSession(), tmp_path, artifact)
    assert not vol.uploads


def test_two_workers_race_and_verify_winner(tmp_path, artifact):
    vol = Volume()
    barrier = threading.Barrier(2)
    original = vol.read_file
    seen = set()
    guard = threading.Lock()
    def read(path):
        with guard:
            first = threading.get_ident() not in seen
            seen.add(threading.get_ident())
        if first:
            # Both saw absence before either starts its publication.
            barrier.wait(timeout=3)
            raise FileNotFoundError(path)
        yield from original(path)
    vol.read_file = read
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: stage_input(vol, SDKSession(), tmp_path, artifact), range(2)))
    assert results[0] == results[1]
    assert vol.uploads == [key(artifact)]
    assert vol.files[key(artifact)] == PNG


def test_failed_upload_not_cached(tmp_path, artifact):
    vol, session = Volume(), SDKSession()
    original = vol.batch_upload
    @contextmanager
    def broken(force=False):
        raise OSError("connection lost")
        yield
    vol.batch_upload = broken
    with pytest.raises(OSError):
        stage_input(vol, session, tmp_path, artifact)
    vol.batch_upload = original
    stage_input(vol, session, tmp_path, artifact)
    assert vol.uploads == [key(artifact)]


def backend(vol, cls=sdk.Backend):
    with patch("runtime.serving.modal_setup.model_volume", return_value=vol):
        obj = cls()
    obj.modal = Mock()
    return obj


def record(call_id="fc-test", status="queued"):
    return {"prefix": "model-results/lab", "id": "001", "intent_hash": "intent",
            "execution_hash": "execution", "call_id": call_id, "status": status}


def completed(job):
    return {"id": job["id"], "file": job["id"] + ".png", "bytes": len(PNG),
            "sha256": hashlib.sha256(PNG).hexdigest(), "status": "completed",
            "intent_hash": job["intent_hash"], "execution_hash": job["execution_hash"]}


def put_record(vol, job, value):
    vol.files[volume_key("/" + job["prefix"] + "/" + job["id"] + ".json")] = json.dumps(value).encode()


def test_pending_call_deduplicates_ledger_and_handle_but_refreshes(tmp_path):
    vol = Volume()
    b = backend(vol)
    job = record()
    b.modal.FunctionCall.from_id.return_value.get.side_effect = TimeoutError()
    with patch("orchestration.sdk_session.time.monotonic", side_effect=[0, 1, 2, 7, 7]):
        for _ in range(4):
            assert b.poll(job) is None
    assert len(vol.reads) == 2
    b.modal.FunctionCall.from_id.assert_called_once_with("fc-test")
    assert b.modal.FunctionCall.from_id.return_value.get.call_count == 4


def test_terminal_call_immediately_rechecks_authoritative_record():
    vol = Volume()
    b = backend(vol)
    job = record()
    call = b.modal.FunctionCall.from_id.return_value
    call.get.side_effect = TimeoutError()
    assert b.poll(job) is None
    result = completed(job)
    put_record(vol, job, result)
    call.get.side_effect = RuntimeError("call expired")
    assert b.poll(job) == result
    assert ("call", job["call_id"]) not in b.session.handles
    assert len(vol.reads) == 2


def test_terminal_success_rechecks_conflicting_persisted_result():
    vol = Volume()
    b = backend(vol)
    job = record()
    call = b.modal.FunctionCall.from_id.return_value
    call.get.side_effect = TimeoutError()
    assert b.poll(job) is None
    put_record(vol, job, {**completed(job), "execution_hash": "wrong"})
    call.get.side_effect = None
    call.get.return_value = completed(job)
    with pytest.raises(ValueError, match="conflict"):
        b.poll(job)


def test_unknown_acceptance_always_observes_ledger_first():
    vol = Volume()
    b = backend(vol)
    job = record(status="uncertain")
    b.modal.FunctionCall.from_id.return_value.get.side_effect = TimeoutError()
    assert b.poll(job) is None
    put_record(vol, job, completed(job))
    assert b.poll(job) == completed(job)
    assert len(vol.reads) == 2
    assert b.modal.FunctionCall.from_id.return_value.get.call_count == 1


def test_workflow_call_handles_are_version_scoped():
    b = backend(Volume(), WorkflowBackend)
    fn = b.modal.Function.from_name.return_value
    fn.spawn.return_value.object_id = "fc-workflow"
    for ident in ("one", "two"):
        b.submit_workflow({}, ident, deployment_version="a" * 64)
    b.submit_workflow({}, "three", deployment_version="b" * 64)
    assert b.modal.Function.from_name.call_count == 2
    assert fn.spawn.call_count == 3
    assert fn.spawn.call_args.kwargs == {"expected_deployment_version": "b" * 64}


def test_same_cloud_frame_reference_reads_once(artifact):
    vol = Volume()
    vol.files[key(artifact)] = PNG
    ref = {"path": "workflow-inputs/" + artifact["sha256"],
           "sha256": artifact["sha256"], "size": artifact["size"]}
    payload = resolve_frames({"first_frame_artifact": ref, "last_frame_artifact": ref}, vol)
    assert vol.reads == [key(artifact)]
    assert base64.b64decode(payload["first_frame_base64"]) == PNG
    assert payload["first_frame_base64"] == payload["last_frame_base64"]


def test_bounded_handle_and_verification_caches():
    session = SDKSession()
    for i in range(600):
        session.handle(("call", i), object)
        session.remember_input(i)
        session.note_ledger_read(i)
    assert len(session.handles) == 256
    assert len(session.inputs) == len(session.observed) == 512
    assert ("call", 0) not in session.handles
    assert not session.input_verified(0)


def test_audit_protects_known_future_inputs_and_never_marks_orphans_deletable(tmp_path, artifact):
    vol = Volume()
    root = tmp_path / "tasks"
    path = root / "project" / "scene" / "plan.json"
    path.parent.mkdir(parents=True)
    missing = {"path": "future.png", "sha256": "c" * 64, "size": 10}
    plan = {"volume": "isolated", "volume_sub_path": VOLUME_SUB_PATH, "jobs": [
        {"key": "future", "inputs": {"first_frame": artifact, "last_frame": missing}},
        {"key": "consumer", "inputs": {"first_frame": {"job": "upstream", "output": "image"}}}]}
    plan["hash"] = intent_digest(plan)
    path.write_text(json.dumps(plan), encoding="utf-8")
    vol.files[key(artifact)] = PNG
    vol.files[volume_key("/workflow-inputs/" + "b" * 64)] = b"unobserved"
    vol.files["/weights/mymodel"] = b"untouched"
    before = dict(vol.files)
    report = audit(vol, "isolated", [root])
    assert report["plans_scanned"] == 1
    assert report["referenced_bytes"] == len(PNG)
    assert report["unobserved_bytes"] == len(b"unobserved")
    assert report["missing_references"][0]["expected_in_volume"] is False
    assert report["deletion_allowed"] is False
    assert vol.files == before and not vol.reads and not vol.uploads
    assert vol.lists == [("inference/workflow-inputs", False)]
    path.write_text(json.dumps({**plan, "hash": "tampered"}), encoding="utf-8")
    report = audit(vol, "isolated", [root])
    assert report["errors"] and report["deletion_allowed"] is False


@pytest.mark.parametrize("matching", [True, False])
def test_sdk_v2_invalid_error_conflict_only_for_exact_path(tmp_path, artifact, matching):
    from modal.exception import InvalidError
    vol, session = Volume(), SDKSession()
    @contextmanager
    def conflict(force=False):
        yield Mock()
        # Simulate a different writer publishing between the miss and commit.
        vol.files[key(artifact)] = PNG
        destination = key(artifact) if matching else "/unrelated/path"
        raise InvalidError(f'path "{destination}" already exists')
    vol.batch_upload = conflict
    if matching:
        assert stage_input(vol, session, tmp_path, artifact)["sha256"] == artifact["sha256"]
        assert vol.reads == [key(artifact), key(artifact)]
    else:
        with pytest.raises(InvalidError):
            stage_input(vol, session, tmp_path, artifact)
        assert not session.inputs
