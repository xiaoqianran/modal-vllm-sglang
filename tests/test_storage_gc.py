"""Managed input retirement/GC, including stale and corrupt root rejection."""
import copy
import hashlib
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from modal.types import InputStatus

from orchestration import sdk, storage_gc as gc, workflow_recovery as recovery
from orchestration.input_store import MANAGED_PREFIX, LIFECYCLE, stage_input
from orchestration.protocol import run_id, state_path, VERSION
from runtime.config.storage import volume_key
from runtime.tasks.task_identity import intent_digest
from runtime.tasks.task_spec import prepare, result_prefix, execution_signature
from tests.test_sdk_reuse import Volume
from tests.test_workflow_recovery import fixture, api, node
from tools import recover_workflow as cli


class TreeVolume(Volume):
    def __init__(self, files):
        super().__init__()
        self.files.update(files)
        self.deleted = []
        self.mtimes = {}

    def iterdir(self, path, recursive=False):
        assert not recursive
        self.lists.append((path, recursive))
        prefix = path.strip("/") + "/"
        children = {}
        for key, data in self.files.items():
            normalized = key.lstrip("/")
            if not normalized.startswith(prefix):
                continue
            name, sep, _ = normalized[len(prefix):].partition("/")
            child = prefix + name
            if sep:
                children[child] = SimpleNamespace(path=child, type=2, size=0, mtime=0)
            else:
                children[child] = SimpleNamespace(path=child, type=1, size=len(data),
                    mtime=self.mtimes.get(key, 100))
        yield from children.values()

    def remove_file(self, path, recursive=False):
        assert not recursive
        self.deleted.append(path)
        del self.files[path]


@pytest.fixture
def lab(fixture):
    f = fixture
    f.vol = TreeVolume(f.vol.files)
    f.backend.volume = f.vol
    f.backend.managed_workflow = True
    return f


def clone(lab, name, *, ref=None, legacy=False):
    payload = copy.deepcopy(lab.payload)
    plan = payload["plan"]
    plan["project"] = plan["name"] = name
    if ref is not None:
        plan["jobs"][-1]["inputs"]["last_frame"] = {**ref, "type": "image", "storage": "volume"}
    if legacy:
        payload.pop("input_lifecycle", None)
        frame = plan["jobs"][-1]["inputs"]["last_frame"]
        frame["path"] = "workflow-inputs/" + frame["sha256"]
    plan["hash"] = intent_digest({k: v for k, v in plan.items() if k != "hash"})
    return recovery.publish_plan(lab.vol, payload, run_id(plan, "001"), "recovery-test")


def managed_image(lab, color):
    from PIL import Image
    stream = io.BytesIO()
    Image.new("RGB", (480, 480), color).save(stream, format="PNG")
    data = stream.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    ref = {"path": MANAGED_PREFIX + digest, "sha256": digest, "size": len(data)}
    lab.vol.files[volume_key("/" + ref["path"])] = data
    return ref


def complete(lab, manifest, *, terminal=True):
    plan = manifest["payload"]["plan"]
    records = copy.deepcopy(manifest["payload"]["initial_jobs"])
    owner = recovery.acquire_owner(lab.vol, manifest, api("fc-" + manifest["identity"][:12]))
    for job in plan["jobs"]:
        cap = sdk.model_capabilities.CAPABILITIES[job["uses"]]
        spec = plan["services"][cap["service"]]["spec"]
        request = sdk.item_for(lab.directory, job, records, spec, lab.backend)
        signature = prepare(spec, request)[1]
        fingerprint = "cache"
        prefix = result_prefix(spec, plan["project"], plan["scene"], request["operation"])
        data = lab.source.read_bytes() if cap["output"] == "image" else b"\x00\x00\x00\x1cftypisom" + b"video" * 30
        suffix = ".png" if cap["output"] == "image" else ".mp4"
        result = {"status": "completed", "id": request["id"], "file": request["id"] + suffix,
                  "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "intent_hash": signature,
                  "execution_hash": execution_signature(signature, fingerprint)}
        receipt = {"id": request["id"], "prefix": prefix, "intent_hash": signature,
                   "execution_hash": result["execution_hash"], "cache_fingerprint": fingerprint,
                   "status": "completed", "result": result}
        receipt["artifacts"] = {cap["output"]: sdk.cloud_artifact(receipt, cap["output"])}
        records[job["key"]] = receipt
        lab.vol.files[volume_key("/" + prefix + "/" + request["id"] + ".json")] = json.dumps(result).encode()
        lab.vol.files[volume_key("/" + prefix + "/" + result["file"])] = data
    checkpoint = {"version": VERSION, "payload_hash": manifest["payload_hash"],
                  "status": "completed", "jobs": records}
    lab.vol.files[volume_key("/" + state_path(manifest["identity"]))] = json.dumps(checkpoint).encode()
    modal_api = api(graph=[node(owner, InputStatus.SUCCESS if terminal else InputStatus.PENDING)])
    return checkpoint, modal_api


def release(lab, manifest):
    checkpoint, modal_api = complete(lab, manifest)
    marker = gc.retire(lab.vol, manifest, modal_api, confirm=True)
    return checkpoint, marker


def snapshot(lab, age=0):
    return gc.plan_gc(lab.vol, "recovery-test", min_age=age, as_of=100000)


def key(ref):
    return volume_key("/" + ref["path"])


def original_ref(lab):
    return {k: lab.payload["plan"]["jobs"][-1]["inputs"]["last_frame"][k]
            for k in ("path", "sha256", "size")}


def test_new_managed_namespace_and_direct_legacy_cache_are_distinct(lab):
    ref = original_ref(lab)
    assert lab.payload["input_lifecycle"] == LIFECYCLE
    assert ref["path"] == MANAGED_PREFIX + ref["sha256"]
    from pathlib import Path
    artifact = {"path": lab.source.name, "sha256": ref["sha256"], "size": ref["size"]}
    legacy = stage_input(lab.vol, lab.backend.session, lab.source.parent, artifact)
    assert legacy["path"] == "workflow-inputs/" + ref["sha256"]
    assert key(legacy) in lab.vol.files and key(ref) in lab.vol.files
    old = copy.deepcopy(lab.payload)
    old.pop("input_lifecycle")
    with pytest.raises(ValueError, match="Invalid managed"):
        recovery.validate_payload(old, lab.identity, "recovery-test")
    lab.backend.managed_workflow = False
    with pytest.raises(ValueError, match="durable workflow controller"):
        lab.backend.submit({}, {"first_frame_artifact": ref}, "", "")


def test_retire_requires_confirmation_completion_and_owner_proof(lab):
    with pytest.raises(ValueError, match="confirmation"):
        gc.retire(lab.vol, lab.manifest, api())
    with pytest.raises(ValueError, match="completed managed"):
        gc.retire(lab.vol, lab.manifest, api(), confirm=True)
    _, modal_api = complete(lab, lab.manifest, terminal=False)
    with pytest.raises(ValueError, match="termination"):
        gc.retire(lab.vol, lab.manifest, modal_api, confirm=True)
    owner = recovery.latest_owner(lab.vol, lab.manifest)
    modal_api = api(graph=[node(owner, InputStatus.SUCCESS)])
    marker = gc.retire(lab.vol, lab.manifest, modal_api, confirm=True)
    assert gc.retire(lab.vol, lab.manifest, api(), confirm=True) == marker
    assert cli.status(lab.vol, lab.manifest, api())["inputs_retired"]


def test_retire_checks_authoritative_gpu_receipts(lab):
    checkpoint, modal_api = complete(lab, lab.manifest)
    record = next(iter(checkpoint["jobs"].values()))
    del lab.vol.files[volume_key("/" + record["prefix"] + "/" + record["id"] + ".json")]
    with pytest.raises(ValueError, match="GPU receipt missing"):
        gc.retire(lab.vol, lab.manifest, modal_api, confirm=True)
    assert recovery.read_retirement(lab.vol, lab.manifest) is None


def test_plan_preserves_live_shared_legacy_orphan_and_output_references(lab):
    shared = original_ref(lab)
    unique = managed_image(lab, "red")
    orphan = managed_image(lab, "green")
    shared_retired = clone(lab, "100-shared", ref=shared)
    unique_retired = clone(lab, "101-unique", ref=unique)
    release(lab, shared_retired)
    release(lab, unique_retired)
    legacy_plan = clone(lab, "102-legacy", ref=unique, legacy=True)
    legacy_key = volume_key("/workflow-inputs/" + unique["sha256"])
    lab.vol.files[legacy_key] = lab.vol.files[key(unique)]
    legacy_id = "f" * 64
    lab.vol.files[volume_key("/" + state_path(legacy_id))] = b'{"version":1}'
    lab.vol.files["/inference/weights/model.bin"] = b"model"
    lab.vol.reads.clear()
    plan = snapshot(lab)
    assert plan["deletion_allowed"] and not plan["errors"]
    assert plan["legacy_state_only_roots"] == [legacy_id]
    assert [obj["path"] for obj in plan["candidates"]] == [unique["path"]]
    by_path = {obj["path"]: obj for obj in plan["objects"]}
    assert by_path[shared["path"]]["status"] == "live"
    assert by_path[shared["path"]]["live"] == [lab.identity]
    assert by_path[orphan["path"]]["status"] == "untracked_protected"
    assert plan["protected_references"]["workflow-inputs/" + unique["sha256"]]["live"] == [legacy_plan["identity"]]
    assert not any(path.startswith(volume_key("/workflow-inputs/")) for path in lab.vol.reads)
    assert not lab.vol.deleted
    assert not snapshot(lab, age=100000)["candidates"]


def test_apply_stale_plan_new_dependency_rejects_before_any_delete(lab):
    release(lab, lab.manifest)
    plan = snapshot(lab)
    assert len(plan["candidates"]) == 1
    clone(lab, "103-new")
    with pytest.raises(RuntimeError, match="snapshot changed"):
        gc.apply_gc(lab.vol, "recovery-test", plan, confirm_quiescent=True)
    assert not lab.vol.deleted


def test_apply_precise_deletion_retains_results_and_redelivery(lab, tmp_path):
    checkpoint, _ = release(lab, lab.manifest)
    ref = original_ref(lab)
    old_key = volume_key("/workflow-inputs/" + ref["sha256"])
    lab.vol.files[old_key] = lab.vol.files[key(ref)]
    orphan = managed_image(lab, "green")
    before = dict(lab.vol.files)
    plan = snapshot(lab)
    result = gc.apply_gc(lab.vol, "recovery-test", plan, confirm_quiescent=True)
    assert result["deleted_bytes"] == ref["size"]
    assert lab.vol.deleted == [key(ref)]
    assert lab.vol.files == {k: v for k, v in before.items() if k != key(ref)}
    assert not snapshot(lab)["candidates"]
    assert snapshot(lab)["deletion_allowed"]
    assert recovery.read_state(lab.vol, lab.manifest) == checkpoint
    assert cli.resume(lab.vol, lab.manifest, api(), "a"*64)["call_id"] is None
    outputs = cli.collect(lab.vol, lab.manifest, tmp_path / "redelivered")
    for artifact in outputs["files"].values():
        assert hashlib.sha256((tmp_path / "redelivered" / artifact["path"]).read_bytes()).hexdigest() == artifact["sha256"]
    assert key(orphan) in lab.vol.files and old_key in lab.vol.files


def test_missing_live_object_blocks_all_deletion(lab):
    unique = managed_image(lab, "red")
    release(lab, clone(lab, "104-unique", ref=unique))
    del lab.vol.files[key(original_ref(lab))]
    plan = snapshot(lab)
    assert not plan["deletion_allowed"] and not plan["candidates"]
    assert any("Live managed input missing" in e["error"] for e in plan["errors"])
    with pytest.raises(ValueError, match="blocked"):
        gc.apply_gc(lab.vol, "recovery-test", plan, confirm_quiescent=True)
    assert not lab.vol.deleted


def test_corrupt_retirement_and_lost_plan_fail_closed(lab):
    _, marker = release(lab, lab.manifest)
    marker_key = volume_key("/" + recovery.retirement_path(lab.identity))
    lab.vol.files[marker_key] = json.dumps({**marker, "owner_hash": "bad"}).encode()
    assert not snapshot(lab)["deletion_allowed"]
    lab.vol.files[marker_key] = json.dumps(marker).encode()
    del lab.vol.files[volume_key("/" + recovery.plan_path(lab.identity))]
    plan = snapshot(lab)
    assert not plan["deletion_allowed"]
    assert "nonlegacy" in plan["errors"][0]["error"]


def test_apply_verifies_hash_even_if_size_and_mtime_match(lab):
    release(lab, lab.manifest)
    plan = snapshot(lab)
    ref = original_ref(lab)
    data = lab.vol.files[key(ref)]
    lab.vol.files[key(ref)] = data[:-1] + bytes([data[-1] ^ 1])
    with pytest.raises(ValueError, match="content mismatch"):
        gc.apply_gc(lab.vol, "recovery-test", plan, confirm_quiescent=True)
    assert not lab.vol.deleted


def test_apply_rejects_unconfirmed_tampered_or_wrong_volume(lab):
    release(lab, lab.manifest)
    plan = snapshot(lab)
    with pytest.raises(ValueError, match="exclusive"):
        gc.apply_gc(lab.vol, "recovery-test", plan)
    wrong = {**plan, "volume_id": "vo-other"}
    wrong["snapshot_hash"] = intent_digest({k: v for k, v in wrong.items() if k != "snapshot_hash"})
    with pytest.raises(ValueError, match="Invalid"):
        gc.apply_gc(lab.vol, "recovery-test", wrong, confirm_quiescent=True)
    tampered = copy.deepcopy(plan)
    tampered["candidates"][0]["path"] = "model-results/protected.png"
    tampered["snapshot_hash"] = intent_digest({k: v for k, v in tampered.items() if k != "snapshot_hash"})
    with pytest.raises(RuntimeError, match="snapshot changed"):
        gc.apply_gc(lab.vol, "recovery-test", tampered, confirm_quiescent=True)
    assert not lab.vol.deleted


def test_retired_workflow_without_completed_checkpoint_cannot_execute(lab):
    release(lab, lab.manifest)
    del lab.vol.files[volume_key("/" + state_path(lab.identity))]
    runner = Mock()
    with pytest.raises(recovery.ControllerBusy, match="Retired"):
        recovery.execute(lab.vol, lab.manifest, api(), runner, recover=True)
    runner.assert_not_called()
    with pytest.raises(recovery.ControllerBusy, match="Retired"):
        cli.resume(lab.vol, lab.manifest, api(), "a"*64)
