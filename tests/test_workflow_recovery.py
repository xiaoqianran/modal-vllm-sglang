"""Persisted cloud plans and exclusive, evidence-based controller recovery."""
import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from modal.types import InputStatus

from orchestration import sdk, state, workflow_recovery as r
from orchestration.protocol import bindings, run_id, state_path, VERSION
from pipeline.s01_build_plan import ROOT, build_plan
from pipeline.s02_submit_shots import payload_for
from pipeline.s03_run_shot import run
from runtime.config.storage import volume_key
from runtime.tasks.task_identity import intent_digest
from runtime.tasks.task_spec import prepare, result_prefix, execution_signature
from tests.test_sdk_reuse import Volume
from tools import recover_workflow as cli


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_VOLUME_NAME", "recovery-test")
    from PIL import Image
    workflow = json.loads((ROOT / "workflows/storyboard-to-video.json").read_text(encoding="utf-8"))
    workflow["id"] = "recovery-lab"
    workflow["steps"] = [workflow["steps"][0], workflow["steps"][2]]
    workflow["steps"][1]["inputs"]["last_frame"] = {"ref": "input.frame"}
    workflow["outputs"].pop("last_frame")
    state.save(tmp_path / "workflows/recovery-lab.json", workflow)
    source = tmp_path / "input.png"
    Image.new("RGB", (480, 480), "blue").save(source)
    task = tmp_path / "tasks/099-recovery.json"
    state.save(task, {"version": 1, "name": "099-recovery", "workflow": "recovery-lab",
                      "items": [{"id": "001", "first_prompt": "shore", "motion_prompt": "pan",
                                 "frame": {"file": "input.png"}}]})
    plan = build_plan(task, root=tmp_path)
    directory = tmp_path / "local"
    for path, entry in plan["imports"].items():
        target = directory / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((tmp_path / entry["source"]).read_bytes())
    state.save(directory / "plan.json", plan)
    state.save(directory / "state.json", {"plan_hash": plan["hash"],
        "endpoints": {k: v["endpoint"] for k, v in bindings(plan).items()},
        "jobs": {job["key"]: {"status": "pending"} for job in plan["jobs"]}})
    vol = Volume()
    with patch("runtime.serving.modal_setup.model_volume", return_value=vol):
        backend = sdk.Backend()
    payload = payload_for(directory, plan, "001", backend)
    identity = run_id(plan, "001")
    manifest = r.publish_plan(vol, payload, identity, "recovery-test")
    return SimpleNamespace(vol=vol, manifest=manifest, identity=identity, payload=payload,
                           directory=directory, source=source, backend=backend)


def api(call="fc-first", input_id="in-first", graph=()):
    modal_api = Mock()
    modal_api.current_function_call_id.return_value = call
    modal_api.current_input_id.return_value = input_id
    modal_api.FunctionCall.from_id.return_value.get_call_graph.return_value = list(graph)
    return modal_api


def node(owner, status):
    return SimpleNamespace(function_call_id=owner["call_id"], input_id=owner["input_id"], status=status)


def checkpoint(f, jobs, status="running"):
    value = {"version": VERSION, "payload_hash": f.manifest["payload_hash"], "status": status, "jobs": jobs}
    f.vol.files[volume_key("/" + state_path(f.identity))] = json.dumps(value).encode()
    return value


def first_completed(f):
    job = f.payload["plan"]["jobs"][0]
    spec = f.payload["plan"]["services"][job["uses"].split(".")[0]]["spec"]
    item = sdk.item_for(f.directory, job, f.payload["initial_jobs"], spec, f.backend)
    _, signature = prepare(spec, item)
    prefix = result_prefix(spec, f.payload["plan"]["project"], f.payload["plan"]["scene"], item["operation"])
    data = f.source.read_bytes()
    result = {"status": "completed", "id": item["id"], "file": item["id"] + ".png",
              "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
              "intent_hash": signature, "execution_hash": execution_signature(signature, "cache")}
    receipt = {"id": item["id"], "prefix": prefix, "intent_hash": signature,
               "execution_hash": result["execution_hash"], "cache_fingerprint": "cache"}
    record = {**receipt, "status": "completed", "result": result}
    record["artifacts"] = {"image": sdk.cloud_artifact(record, "image")}
    f.vol.files[volume_key("/" + prefix + "/" + item["id"] + ".json")] = json.dumps(result).encode()
    f.vol.files[volume_key("/" + prefix + "/" + result["file"])] = data
    return job, record


def test_persisted_plan_covers_future_local_input_and_survives_source_loss(fixture):
    f = fixture
    original = copy.deepcopy(f.manifest)
    f.source.unlink()
    assert r.load_plan(f.vol, f.identity, "recovery-test") == original
    assert r.publish_plan(f.vol, f.payload, f.identity, "recovery-test") == original
    ref = f.payload["plan"]["jobs"][-1]["inputs"]["last_frame"]
    assert ref["storage"] == "volume"
    assert hashlib.sha256(f.vol.files[volume_key("/" + ref["path"])]).hexdigest() == ref["sha256"]
    changed = copy.deepcopy(f.payload)
    changed["plan"]["jobs"][0]["inputs"]["prompt"] = "changed"
    changed["plan"]["hash"] = intent_digest({k: v for k, v in changed["plan"].items() if k != "hash"})
    with pytest.raises(ValueError, match="different content"):
        r.publish_plan(f.vol, changed, f.identity, "recovery-test")


def test_rejects_wrong_volume_and_tampered_plan(fixture):
    f = fixture
    with pytest.raises(ValueError):
        r.load_plan(f.vol, f.identity, "wrong-volume")
    key = volume_key("/" + r.plan_path(f.identity))
    corrupt = copy.deepcopy(f.manifest)
    corrupt["payload"]["plan"]["jobs"][0]["inputs"]["prompt"] = "tamper"
    f.vol.files[key] = json.dumps(corrupt).encode()
    with pytest.raises(ValueError, match="identity"):
        r.load_plan(f.vol, f.identity, "recovery-test")


def test_live_controller_blocks_takeover_and_expiry_is_not_proof(fixture):
    f = fixture
    owner = r.acquire_owner(f.vol, f.manifest, api())
    active = api("fc-next", "in-next", [node(owner, InputStatus.PENDING)])
    with pytest.raises(r.ControllerBusy):
        r.acquire_owner(f.vol, f.manifest, active, recover=True)
    active.FunctionCall.from_id.return_value.get_call_graph.side_effect = RuntimeError("expired")
    with pytest.raises(r.ControllerBusy):
        r.acquire_owner(f.vol, f.manifest, active, recover=True)
    assert r.latest_owner(f.vol, f.manifest) == owner


@pytest.mark.parametrize("status", [InputStatus.SUCCESS, InputStatus.FAILURE, InputStatus.INIT_FAILURE,
                                    InputStatus.TERMINATED, InputStatus.TIMEOUT])
def test_only_matching_terminal_input_is_proof(fixture, status):
    f = fixture
    owner = r.acquire_owner(f.vol, f.manifest, api())
    unrelated = node({**owner, "input_id": "in-other"}, status)
    assert not r.owner_terminal(api(graph=[unrelated]), owner)
    assert r.owner_terminal(api(graph=[node(owner, status)]), owner)
    assert not r.owner_terminal(api(graph=[node(owner, status), node(owner, status)]), owner)


def test_concurrent_successors_one_winner(fixture):
    f = fixture
    previous = r.acquire_owner(f.vol, f.manifest, api())
    barrier = Barrier(2)
    def contender(n):
        a = api(f"fc-next-{n}", f"in-next-{n}", [node(previous, InputStatus.TERMINATED)])
        def proof():
            barrier.wait(timeout=5)
            return [node(previous, InputStatus.TERMINATED)]
        a.FunctionCall.from_id.return_value.get_call_graph.side_effect = proof
        try:
            return r.acquire_owner(f.vol, f.manifest, a, recover=True)
        except r.ControllerBusy:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(contender, range(2)))
    winners = [v for v in results if v]
    assert len(winners) == 1
    assert r.latest_owner(f.vol, f.manifest) == winners[0]
    assert winners[0]["parent"] == intent_digest(previous)


def test_legacy_claim_and_orphan_checkpoint_not_taken_over(fixture):
    f = fixture
    registry = Mock()
    registry.put.return_value = False
    registry.get.return_value = f.manifest["payload_hash"]
    with pytest.raises(r.ControllerBusy, match="Legacy"):
        r.acquire_owner(f.vol, f.manifest, api(), recover=True, claim_registry=registry)
    checkpoint(f, f.payload["initial_jobs"])
    with pytest.raises(r.ControllerBusy, match="without durable owner"):
        r.acquire_owner(f.vol, f.manifest, api(), recover=True)


def test_recovery_passes_authoritative_checkpoint_not_initial_jobs(fixture):
    f = fixture
    prior = r.acquire_owner(f.vol, f.manifest, api())
    job, completed = first_completed(f)
    records = copy.deepcopy(f.payload["initial_jobs"])
    records[job["key"]] = completed
    value = checkpoint(f, records)
    runner = Mock(return_value={"status": "done"})
    assert r.execute(f.vol, f.manifest, api("fc-next", "in-next", [node(prior, InputStatus.FAILURE)]),
                     runner, recover=True) == {"status": "done"}
    runner.assert_called_once_with(f.payload, f.identity, durable=True, previous=value)


@pytest.mark.parametrize("previous_status", ["submitting", "pending"])
def test_recovered_submitting_job_observes_old_result_and_never_respawns(fixture, tmp_path, previous_status):
    f = fixture
    job, completed = first_completed(f)
    records = copy.deepcopy(f.payload["initial_jobs"])
    records[job["key"]] = {k: v for k, v in completed.items() if k not in {"result", "artifacts"}}
    records[job["key"]]["status"] = "submitting"
    if previous_status == "pending":
        records[job["key"]] = {"status": "pending"}
    previous = checkpoint(f, records)
    backend = f.backend
    backend.fingerprint = lambda spec: "cache"
    submissions = []
    def submit(spec, item, prefix, fingerprint, **kwargs):
        assert item["_telemetry"]["job_key"] == "video/001"
        assert item["first_frame_artifact"]["sha256"] == completed["result"]["sha256"]
        assert item["last_frame_artifact"]["path"] == f.payload["plan"]["jobs"][-1]["inputs"]["last_frame"]["path"]
        submissions.append(item["id"])
        signature = prepare(spec, item)[1]
        result = {"id": item["id"], "file": item["id"] + ".mp4", "bytes": 36,
                  "sha256": "a" * 64, "intent_hash": signature,
                  "execution_hash": execution_signature(signature, fingerprint), "status": "completed"}
        f.vol.files[volume_key("/" + prefix + "/" + item["id"] + ".json")] = json.dumps(result).encode()
        return "fc-video"
    backend.submit = submit
    f.vol.commit = Mock()
    with patch("pipeline.s03_run_shot.cloud_recorder") as recorder:
        from runtime.telemetry.recorder import Recorder
        recorder.return_value = Recorder()
        result = run(f.payload, f.identity, backend=backend, root=tmp_path / "remote",
                     durable=True, previous=previous, timeout=10, poll=0.001)
    assert result["status"] == "completed"
    assert len(submissions) == 1
    assert result["jobs"][job["key"]]["result"] == completed["result"]


def test_unknown_submission_is_never_replayed(fixture, tmp_path):
    f = fixture
    job, completed = first_completed(f)
    records = copy.deepcopy(f.payload["initial_jobs"])
    records[job["key"]] = {k: v for k, v in completed.items() if k not in {"result", "artifacts"}}
    records[job["key"]]["status"] = "submitting"
    f.vol.files.pop(volume_key("/" + completed["prefix"] + "/" + completed["id"] + ".json"))
    f.backend.submission_ledger = lambda: SimpleNamespace(lookup=lambda *args: {"status": "submitting"})
    f.backend.submit = Mock(side_effect=AssertionError("must not resubmit unknown acceptance"))
    f.vol.commit = Mock()
    previous = checkpoint(f, records)
    with patch("pipeline.s03_run_shot.cloud_recorder") as recorder:
        from runtime.telemetry.recorder import Recorder
        recorder.return_value = Recorder()
        result = run(f.payload, f.identity, backend=f.backend, root=tmp_path / "remote",
                     durable=True, previous=previous, timeout=0.02, poll=0.001)
    assert result["status"] == "uncertain"
    assert result["jobs"][job["key"]]["status"] == "uncertain"
    f.backend.submit.assert_not_called()


def test_status_and_resume_are_explicit_and_version_guarded(fixture):
    f = fixture
    a = api()
    assert cli.status(f.vol, f.manifest, a)["status"] == "prepared"
    a.App.lookup.return_value.info.return_value.functions = {"resume_shot_" + "a"*32: object()}
    a.Function.from_name.return_value.spawn.return_value.object_id = "fc-resume"
    assert cli.resume(f.vol, f.manifest, a, "a"*64)["call_id"] == "fc-resume"
    a.Function.from_name.return_value.spawn.assert_called_once_with(
        f.identity, f.manifest["payload_hash"], expected_deployment_version="a"*64)
    owner = r.acquire_owner(f.vol, f.manifest, api())
    a.FunctionCall.from_id.return_value.get_call_graph.return_value = [node(owner, InputStatus.PENDING)]
    with pytest.raises(r.ControllerBusy):
        cli.resume(f.vol, f.manifest, a, "a"*64)
    assert a.Function.from_name.return_value.spawn.call_count == 1

def test_real_sdk_input_execution_suffix_matches_exact_base_only():
    owner = {"call_id": "fc-test", "input_id": "in-01TEST:1795617479305-0"}
    terminal = SimpleNamespace(function_call_id="fc-test", input_id="in-01TEST",
                               status=InputStatus.FAILURE)
    assert r.owner_terminal(api(graph=[terminal]), owner)
    assert not r.owner_terminal(api(graph=[terminal]), {**owner, "call_id": "fc-other"})
    assert not r.owner_terminal(api(graph=[terminal]), {**owner, "input_id": "in-01TEST2:1795617479305-0"})
    assert not r.owner_terminal(api(graph=[terminal]), {**owner, "input_id": "in-01TEST:unknown"})

def test_terminal_evidence_survives_function_removal(fixture):
    f = fixture
    prior = r.acquire_owner(f.vol, f.manifest, api())
    terminal = api(graph=[node(prior, InputStatus.FAILURE)])
    evidence = r.seal_terminal(f.vol, f.manifest, terminal, prior)
    assert evidence["status"] == "FAILURE"
    unavailable = api("fc-next", "in-next")
    unavailable.FunctionCall.from_id.return_value.get_call_graph.side_effect = RuntimeError("Function not found")
    next_owner = r.acquire_owner(f.vol, f.manifest, unavailable, recover=True)
    assert next_owner["parent"] == intent_digest(prior)
    assert r.terminal_evidence(f.vol, f.manifest, prior) == evidence
    unavailable.FunctionCall.from_id.assert_not_called()


def test_tampered_terminal_evidence_fails_closed(fixture):
    f = fixture
    prior = r.acquire_owner(f.vol, f.manifest, api())
    r.seal_terminal(f.vol, f.manifest, api(graph=[node(prior, InputStatus.FAILURE)]), prior)
    key = volume_key("/" + r.terminal_path(f.identity, prior))
    record = json.loads(f.vol.files[key])
    record["call_id"] = "fc-unrelated"
    f.vol.files[key] = json.dumps(record).encode()
    with pytest.raises(ValueError, match="terminal evidence"):
        r.acquire_owner(f.vol, f.manifest, api("fc-next","in-next"), recover=True)


def test_local_runner_detects_dead_controller_without_resubmitting_gpu(fixture):
    from pipeline.s02_submit_shots import Backend as WorkflowBackend
    f = fixture
    owner = r.acquire_owner(f.vol, f.manifest, api())
    checkpoint(f, f.payload["initial_jobs"])
    with patch("runtime.serving.modal_setup.model_volume", return_value=f.vol):
        b = WorkflowBackend()
    b.modal = api(graph=[node(owner, InputStatus.FAILURE)])
    receipt = {"id": f.identity, "payload_hash": f.manifest["payload_hash"],
               "call_id": owner["call_id"], "status": "running"}
    result = b.poll_workflow(receipt)
    assert result["status"] == "uncertain"
    assert result["controller_state"] == "terminal"
    assert result["jobs"] == f.payload["initial_jobs"]
    assert r.read_state(f.vol, f.manifest)["status"] == "running"
    assert r.terminal_evidence(f.vol, f.manifest, owner)
    assert b.poll_workflow({**receipt, **result})["status"] == "uncertain"
    assert b.modal.FunctionCall.from_id.return_value.get_call_graph.call_count == 1
    b.modal.Function.from_name.assert_not_called()
    b.modal.Cls.from_name.assert_not_called()


@pytest.mark.parametrize("known_active", [True, False])
def test_local_runner_does_not_treat_unknown_controller_as_dead(fixture, known_active):
    from pipeline.s02_submit_shots import Backend as WorkflowBackend
    f = fixture
    owner = r.acquire_owner(f.vol, f.manifest, api())
    checkpoint(f, f.payload["initial_jobs"])
    with patch("runtime.serving.modal_setup.model_volume", return_value=f.vol):
        b = WorkflowBackend()
    b.modal = api(graph=[node(owner, InputStatus.PENDING)] if known_active else [])
    receipt = {"id": f.identity, "payload_hash": f.manifest["payload_hash"],
               "call_id": owner["call_id"], "status": "running"}
    result = b.poll_workflow(receipt)
    assert result["status"] == ("running" if known_active else "uncertain")
    assert result["controller_state"] == ("active" if known_active else "unconfirmed")
    assert r.terminal_evidence(f.vol, f.manifest, owner) is None
