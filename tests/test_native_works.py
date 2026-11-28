"""Offline regression coverage for the native HF work-directory contract."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from orchestration.studio_works import list_works, work_detail
from runtime.archive.huggingface import public_url
from runtime.archive.works import (derivative_path, media_path, parse_media,
                                   task_definition, task_path, validate_task, work_id)
from runtime.delivery.outbox import validate_receipt
from tools.migrate_native_works import migration_plan, apply_migration

BUCKET = "example/public"
DIGEST = "a" * 64


def fixture():
    job = {"key": "source/001", "step": "source", "item": "001",
           "uses": "moss.t2i", "params": {"size": "1024x1024"},
           "inputs": {"prompt": "a quiet landscape", "seed": 123,
                      "image": {"path": "/private/input.png"}}}
    plan = {"version": 1, "name": "029-landscape", "project": "029-landscape",
            "scene": "001-main", "workflow": "moss-t2i", "jobs": [job],
            "delivery": {"provider": "huggingface", "bucket": BUCKET},
            "imports": {"private": {"source": "D:/private"}},
            "services": {"private": {"secret": "do-not-publish"}}}
    return plan, job


def test_work_names_are_deterministic_and_unambiguous():
    plan, job = fixture()
    identity = work_id(plan)
    assert work_id({**plan, "hash": "transient", "work": {"task": {}}}) == identity
    assert work_id({**plan, "jobs": [{**job, "inputs": {"prompt": "different"}}]}) != identity
    original = media_path({"id": identity, "item": "001", "step": "source"}, ".png")
    assert original == f"works/{identity}/001/source.png"
    assert derivative_path(original) == f"works/{identity}/001/source.preview.webp"
    assert derivative_path(original.removesuffix(".png") + ".mp4").endswith("/source.poster.webp")
    assert parse_media(derivative_path(original)) is None
    with pytest.raises(ValueError):
        media_path({"id": identity, "item": "../secret", "step": "source"}, ".png")


def test_task_definition_excludes_private_execution_metadata():
    plan, _ = fixture()
    definition = validate_task(work_id(plan), task_definition(plan))
    assert definition["jobs"][0]["inputs"] == {"prompt": "a quiet landscape", "seed": 123}
    assert "services" not in definition and "imports" not in definition
    with pytest.raises(ValueError):
        validate_task(work_id(plan), {**definition, "jobs": definition["jobs"] * 2})


def test_directory_enumeration_is_nonrecursive_and_does_not_load_media():
    plan, _ = fixture()
    identity = work_id(plan)
    tree = Mock(return_value=[
        SimpleNamespace(type="directory", path=f"works/{identity}"),
        SimpleNamespace(type="directory", path="works/invalid"),
        SimpleNamespace(type="file", path=f"works/{identity}/task.json", size=5),
    ])
    assert list_works(BUCKET, tree=tree) == [{"id": identity, "name": plan["name"]}]
    tree.assert_called_once_with(BUCKET, "works", False)


def test_work_detail_uses_actual_files_and_optional_sibling_preview():
    plan, job = fixture()
    plan["jobs"].append({**job, "item": "002", "key": "source/002"})
    identity = work_id(plan)
    prefix = f"works/{identity}"
    original = prefix + "/001/source.png"
    preview = derivative_path(original)
    tree = Mock(return_value=[SimpleNamespace(type="file", path=path, size=123)
                              for path in (task_path(identity), original, preview,
                                           prefix + "/999/source.png",
                                           prefix + "/002/source.preview.webp")])
    load = Mock(return_value=task_definition(plan))
    detail = work_detail(BUCKET, identity, tree=tree, load_task=load)
    assert detail["expected"] == 2 and detail["published"] == 1
    asset = detail["assets"][0]
    assert asset["url"] == public_url(BUCKET, original)
    assert asset["thumb"] == public_url(BUCKET, preview)
    assert asset["prompt"] == "a quiet landscape"
    tree.assert_called_once_with(BUCKET, prefix, True)
    load.assert_called_once_with(BUCKET, identity)


def test_no_preview_falls_back_to_original_and_duplicate_originals_fail_closed():
    plan, _ = fixture()
    identity = work_id(plan)
    original = f"works/{identity}/001/source.png"
    rows = [SimpleNamespace(type="file", path=p, size=123)
            for p in (task_path(identity), original)]
    read = lambda *_: task_definition(plan)
    detail = work_detail(BUCKET, identity, tree=lambda *_: rows, load_task=read)
    assert detail["assets"][0]["thumb"] == detail["assets"][0]["url"]
    rows.append(SimpleNamespace(type="file", path=original.removesuffix(".png") + ".webp", size=456))
    with pytest.raises(ValueError, match="Ambiguous"):
        work_detail(BUCKET, identity, tree=lambda *_: rows, load_task=read)


def test_receipts_bind_semantic_path_without_breaking_legacy_contract():
    plan, _ = fixture()
    work = {"id": work_id(plan), "item": "001", "step": "source"}
    source = {"type": "image", "sha256": DIGEST, "size": 123,
              "storage": "volume", "path": "model-results/example/result.png"}
    request = {"artifact": source, "delivery": plan["delivery"]}
    legacy_path = f"assets/sha256/aa/{DIGEST}.png"
    row = {"status": "archived", "verification": "sha256", "bucket": BUCKET,
           "source_artifact": source, "original_sha256": DIGEST, "original_bytes": 123,
           "stored_sha256": DIGEST, "stored_bytes": 123, "kind": "image",
           "path": legacy_path, "url": public_url(BUCKET, legacy_path)}
    assert validate_receipt(row, request) == row
    native = media_path(work, ".png")
    row = {**row, "path": native, "url": public_url(BUCKET, native)}
    assert validate_receipt(row, {**request, "work": work}) == row
    with pytest.raises(ValueError):
        validate_receipt(row, {**request, "work": {**work, "item": "002"}})
    with pytest.raises(ValueError):
        validate_receipt(row, request)


def test_historical_migration_is_pure_and_keeps_source_addresses():
    plan, job = fixture()
    old_path = f"assets/sha256/aa/{DIGEST}.png"
    artifact = {"sha256": DIGEST, "size": 123}
    receipt = {"status": "archived", "verification": "sha256", "bucket": BUCKET,
               "path": old_path, "url": public_url(BUCKET, old_path), "kind": "image",
               "stored_sha256": DIGEST, "stored_bytes": 123,
               "original_sha256": DIGEST, "original_bytes": 123}
    current = {"jobs": {job["key"]: {"artifacts": {"image": artifact}, "cloud_archive": receipt}}}
    result = migration_plan(plan, current)
    assert result["copies"] == [{"source": old_path, "target": f"works/{work_id(plan)}/001/source.png",
                                  "size": 123, "sha256": DIGEST}]
    assert receipt["path"] == old_path
    assert result["missing"] == []


def test_migration_refuses_wrong_bucket():
    with pytest.raises(ValueError, match="bucket"):
        apply_migration({"bucket": BUCKET, "copies": []}, Mock(bucket="other/public"))

def _native_delivery(plan, job, locator):
    from runtime.delivery import outbox, v3
    from tests.test_durable_delivery import SETTINGS
    plan = {**plan, "delivery": SETTINGS, "delivery_protocol": v3.PROTOCOL,
            "delivery_volume": "test-ledger"}
    plan["work"] = {"id": work_id(plan)}
    intent = outbox.make_intent(job, locator, plan)
    return plan, intent


def _native_receipt(request):
    from tests.test_durable_delivery import make_receipt
    row = make_receipt(request)
    target = media_path(request["work"], ".png")
    return {**row, "path": target, "url": public_url(row["bucket"], target)}


def test_legacy_and_native_delivery_share_recovery_ledger():
    from runtime.delivery import outbox, v3
    from tests.test_delivery_v3 import setup
    from tests.test_durable_delivery import make_receipt

    source, store, legacy, _ = setup()
    plan, job = fixture()
    plan, native = _native_delivery(plan, job, legacy["locator"])
    assert native["id"] != legacy["id"]
    assert "work" not in legacy
    outbox.immutable(store, task_path(native["work"]["id"]), task_definition(plan))
    v3.register(store, native)
    legacy_request = v3.ready(store, source, legacy["id"])[1]
    native_request = v3.ready(store, source, native["id"])[1]
    assert legacy_request["artifact"] == native_request["artifact"]
    assert "work" not in legacy_request
    assert native_request["work_task"] == task_definition(plan)
    old_receipt = make_receipt(legacy_request)
    new_receipt = _native_receipt(native_request)
    v3.save_receipts(store, [(legacy, legacy_request, old_receipt),
                             (native, native_request, new_receipt)])
    assert v3.ready(store, source, legacy["id"]) is None
    assert v3.ready(store, source, native["id"]) is None
    assert outbox.receipt(store, legacy, legacy_request) == old_receipt
    assert outbox.receipt(store, native, native_request) == new_receipt
    assert set(v3.active_ids(store, True)) == {legacy["id"], native["id"]}


def test_missing_definition_retries_without_new_generation(monkeypatch):
    from runtime.delivery import outbox, v3
    from tests.test_delivery_v3 import setup

    source, store, legacy, _ = setup()
    plan, job = fixture()
    plan, native = _native_delivery(plan, job, legacy["locator"])
    v3.register(store, native)
    assert v3.ready(store, source, native["id"]) is None
    attempt, failure = outbox.attempts(store, native["id"])
    assert attempt == 1 and failure["kind"] == "retry"
    outbox.immutable(store, task_path(native["work"]["id"]), task_definition(plan))
    monkeypatch.setattr(outbox.time, "time", lambda: failure["retry_at"] + 1)
    recovered, request, attempt = v3.ready(store, source, native["id"])
    assert recovered == native and attempt == 1
    assert request["artifact"]["sha256"] == outbox.resolve(source, legacy)["artifact"]["sha256"]


def test_complete_definition_written_once_before_shot_split(monkeypatch):
    from orchestration.sdk import Backend
    from orchestration.sdk_session import SDKSession
    from runtime.delivery import outbox
    from tests.test_delivery_v3 import Metadata

    plan, job = fixture()
    plan["jobs"].append({**job, "item": "002", "key": "source/002"})
    plan, native = _native_delivery(plan, job, {
        "id": "j1", "prefix": "model-results/moss/demo",
        "intent_hash": "a" * 64, "execution_hash": "b" * 64})
    store = Metadata()
    backend = object.__new__(Backend)
    backend.session = SDKSession()
    backend.delivery_store = lambda _: store
    write = Mock(wraps=outbox.immutable)
    monkeypatch.setattr(outbox, "immutable", write)
    backend.ensure_work_definition(plan)
    backend.ensure_work_definition(plan)
    backend.ensure_work_definition({**plan, "jobs": [job]})
    assert write.call_count == 1
    assert outbox.read(store, task_path(native["work"]["id"])) == task_definition(plan)


def _mounted_request(tmp_path, raw, index, work, definition):
    from hashlib import sha256
    from tests.test_durable_delivery import SETTINGS
    relative = f"model-results/moss/native-test/j{index}.png"
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return {"artifact": {"type": "image", "storage": "volume", "path": relative,
                         "sha256": sha256(raw).hexdigest(), "size": len(raw)},
            "delivery": SETTINGS, "project": "029-landscape",
            "work": work, "work_task": definition}


def _provider(bucket):
    provider = Mock(bucket=bucket)
    provider.publish.return_value = ("task", "sha256", False)
    provider.publish_many.side_effect = lambda entries, **kwargs: {
        path: (public_url(bucket, path), "sha256", False) for path in entries}
    return provider


def test_native_original_batch_keeps_paths_and_no_index(tmp_path):
    from runtime.delivery.originals import publish_originals
    from tests.test_durable_delivery import PNG, SETTINGS

    plan, job = fixture()
    plan["jobs"].append({**job, "item": "002", "key": "source/002"})
    identity = work_id(plan)
    requests = [_mounted_request(tmp_path, PNG, i,
                                {"id": identity, "item": f"{i:03}", "step": "source"},
                                task_definition(plan)) for i in (1, 2)]
    provider = _provider(SETTINGS["bucket"])
    rows = publish_originals(tmp_path, requests, provider_factory=lambda _: provider)
    assert len(rows) == 2 and all("receipt" in row for row in rows)
    assert provider.publish.call_count == 1
    entries = provider.publish_many.call_args.args[0]
    assert set(entries) == {media_path(r["work"], ".png") for r in requests}
    assert provider.publish_many.call_args.kwargs["indexes"] == {}
    for row, request in zip(rows, requests):
        assert validate_receipt(row["receipt"], request) == row["receipt"]


def test_conflicting_work_slot_fails_without_blocking_sibling(tmp_path):
    from runtime.delivery.originals import publish_originals
    from tests.test_durable_delivery import PNG, SETTINGS

    plan, job = fixture()
    plan["jobs"].append({**job, "item": "002", "key": "source/002"})
    identity = work_id(plan)
    a = {"id": identity, "item": "001", "step": "source"}
    b = {**a, "item": "002"}
    definition = task_definition(plan)
    requests = [_mounted_request(tmp_path, raw, i, slot, definition)
                for i, (raw, slot) in enumerate([(PNG, a), (PNG + b"x", a), (PNG, b)])]
    provider = _provider(SETTINGS["bucket"])
    rows = publish_originals(tmp_path, requests, provider_factory=lambda _: provider)
    assert [row.get("kind") for row in rows[:2]] == ["blocked_integrity"] * 2
    assert "receipt" in rows[2]
    assert set(provider.publish_many.call_args.args[0]) == {media_path(b, ".png")}


@pytest.mark.parametrize("kind,ext,module_name,prepare_name,lookup_name,run_name,status,field", [
    ("image", ".png", "preview", "prepare_preview", "lookup_preview", "backfill_batch", "preview", "preview"),
    ("video", ".mp4", "video_poster", "prepare_video_poster", "lookup_video_poster",
     "backfill_video_batch", "poster", "poster"),
])
def test_native_derivatives_are_sibling_files_without_index(tmp_path, monkeypatch,
        kind, ext, module_name, prepare_name, lookup_name, run_name, status, field):
    from importlib import import_module
    from hashlib import sha256
    from runtime.delivery.outbox import validate_derivative

    plan, _ = fixture()
    path = media_path({"id": work_id(plan), "item": "001", "step": "source"}, ext)
    original = tmp_path / ("original" + ext)
    original.write_bytes(b"source")
    digest = sha256(original.read_bytes()).hexdigest()
    derivative = tmp_path / "candidate.webp"
    derivative.write_bytes(b"preview")
    checksum = sha256(derivative.read_bytes()).hexdigest()
    row = {"kind": kind, "status": "archived", "verification": "sha256",
           "bucket": BUCKET, "path": path, "url": public_url(BUCKET, path),
           "stored_bytes": original.stat().st_size, "stored_sha256": digest}
    mod = import_module("runtime.archive." + module_name)
    provider = _provider(BUCKET)
    monkeypatch.setattr("runtime.archive.huggingface.HuggingFaceBucket", lambda _: provider)
    lookup = Mock(side_effect=AssertionError("Native works must not use a global derivative index"))
    monkeypatch.setattr(mod, lookup_name, lookup)
    monkeypatch.setattr(mod, prepare_name, lambda *args: (
        derivative, {"path": "legacy.webp", "sha256": checksum,
                     "bytes": derivative.stat().st_size, "dimensions": [4, 4]}))
    run = getattr(mod, run_name)
    result = run(BUCKET, [row], local_inputs={(BUCKET, digest, row["stored_bytes"]): original})[row["url"]]
    assert result["status"] == status
    assert result[field]["path"] == derivative_path(path)
    assert validate_derivative(result, row)["derivative_status"] == "complete"
    lookup.assert_not_called()
    assert provider.publish_many.call_args.kwargs["indexes"] == {}


def test_cross_device_browsing_ignores_missing_or_ambiguous_local_record(monkeypatch):
    from orchestration.studio_server import StudioService

    plan, _ = fixture()
    identity = work_id(plan)
    detail = {"id": identity, "name": plan["name"], "assets": []}
    monkeypatch.setattr("orchestration.studio_works.work_detail", lambda *_: detail)
    service = object.__new__(StudioService)
    service.works_bucket = lambda: BUCKET
    service._directory = Mock(side_effect=ValueError("Ambiguous local record"))
    assert service.work(identity) == detail


def test_single_item_migration_keeps_full_definition_and_original_receipts():
    plan, job = fixture()
    plan["jobs"].append({**job, "key": "source/002", "item": "002"})
    path = f"assets/sha256/aa/{DIGEST}.png"
    receipt = {"status": "archived", "verification": "sha256", "bucket": BUCKET,
               "path": path, "url": public_url(BUCKET, path), "kind": "image",
               "stored_sha256": DIGEST, "stored_bytes": 123,
               "original_sha256": DIGEST, "original_bytes": 123}
    record = {"cloud_archive": receipt, "artifacts": {"image": {"sha256": DIGEST, "size": 123}}}
    current = {"jobs": {"source/001": record, "source/002": {}}}
    result = migration_plan(plan, current, item="001")
    assert len(result["task"]["jobs"]) == 2
    assert len(result["copies"]) == 1 and not result["missing"]
    assert migration_plan(plan, current)["missing"] == ["source/002"]
    assert receipt["path"] == path
    with pytest.raises(ValueError, match="Unknown migration item"):
        migration_plan(plan, current, item="999")


def test_copy_recovers_lost_acknowledgement_and_never_overwrites(monkeypatch):
    from urllib.error import HTTPError
    from runtime.archive.works import copy_verified

    provider = Mock(bucket=BUCKET, token="fake")
    source = f"assets/sha256/aa/{DIGEST}.png"
    plan, _ = fixture()
    target = media_path({"id": work_id(plan), "item": "001", "step": "source"}, ".png")
    missing = HTTPError(public_url(BUCKET, target), 404, "missing", None, None)
    provider.verify_digest.side_effect = ["sha256", missing, "sha256"]
    meta = SimpleNamespace(size=123, xet_file_data=SimpleNamespace(file_hash="xet-hash"))
    monkeypatch.setattr("huggingface_hub.get_bucket_file_metadata", Mock(return_value=meta))
    copy = Mock(side_effect=TimeoutError("reply lost"))
    monkeypatch.setattr("huggingface_hub.batch_bucket_files", copy)
    assert copy_verified(provider, source, target, 123, DIGEST) == "copied"
    copy.assert_called_once_with(BUCKET, copy=[("bucket", BUCKET, "xet-hash", target)], token="fake")
    copy.reset_mock()
    provider.verify_digest.side_effect = ["sha256", RuntimeError("HF remote SHA256 differs")]
    with pytest.raises(RuntimeError, match="SHA256 differs"):
        copy_verified(provider, source, target, 123, DIGEST)
    copy.assert_not_called()


def test_legacy_catalog_skips_native_media_but_keeps_task_summary(tmp_path):
    import json
    from orchestration.studio import collect

    plan, _ = fixture()
    identity = work_id(plan)
    work = {"id": identity, "item": "001", "step": "source"}
    native = public_url(BUCKET, media_path(work, ".png"))
    legacy = public_url(BUCKET, f"assets/sha256/aa/{DIGEST}.png")
    other = public_url("other/public", media_path(work, ".png"))
    runtime = tmp_path / ".runtime" / "monitor"
    runtime.mkdir(parents=True)
    snapshot = {"name": plan["name"], "task_id": f"{plan['project']}/{plan['scene']}",
                "status": "completed", "jobs": [
                    {"key": f"source/{i:03}", "item": f"{i:03}", "step": "source",
                     "asset": {"url": url, "kind": "image", "filename": "source.png"}}
                    for i, url in enumerate((native, legacy, other), 1)]}
    (runtime / (plan["name"] + ".js")).write_text(
        "window.updateTaskMonitor(" + json.dumps(snapshot) + ");", encoding="utf-8")
    data = collect(tmp_path / "outputs", thumbs=False, native_bucket=BUCKET)
    assert {a["url"] for a in data["assets"]} == {legacy, other}
    assert data["tasks"][0]["snapshot"]["task_id"] == snapshot["task_id"]
    # Static legacy entrypoints retain their original compatibility behavior.
    old = collect(tmp_path / "outputs", thumbs=False)
    assert {a["url"] for a in old["assets"]} == {native, legacy, other}


def test_work_detail_links_only_matching_local_execution(monkeypatch):
    from orchestration.studio_server import StudioService

    plan, job = fixture()
    identity = work_id(plan)
    original = media_path({"id": identity, "item": job["item"], "step": job["step"]}, ".png")
    detail = {"id": identity, "name": plan["name"], "assets": [
        {"id": identity + "/" + job["key"], "url": public_url(BUCKET, original), "bytes": 123}
    ]}
    artifact = {"type": "image", "storage": "volume", "path": "model-results/moss/j.png",
                "size": 123, "sha256": DIGEST}
    receipt = {"status": "archived", "verification": "sha256", "bucket": BUCKET,
               "path": original, "original_bytes": 123, "stored_bytes": 123,
               "original_sha256": DIGEST}
    current = {"jobs": {job["key"]: {"artifacts": {"image": artifact}, "cloud_archive": receipt}}}
    monkeypatch.setattr("orchestration.studio_works.work_detail", lambda *_: detail)
    monkeypatch.setattr("orchestration.sdk.snapshot", lambda _: (plan, current))
    service = object.__new__(StudioService)
    service.works_bucket = lambda: BUCKET
    service._directory = lambda _: "local-scene"
    result = service.work(identity)
    assert result["local_task_id"] == f"{plan['project']}/{plan['scene']}"
    assert result["assets"][0]["artifact"] == {k: artifact[k] for k in ("path", "sha256", "size")}
    # An unrelated local task must not attach an edit source or task link.
    result.pop("local_task_id")
    result["assets"][0].pop("artifact")
    monkeypatch.setattr("orchestration.sdk.snapshot", lambda _: ({**plan, "scene": "002-other"}, current))
    result = service.work(identity)
    assert "local_task_id" not in result and "artifact" not in result["assets"][0]
    # A known task with inconsistent archive size is not a trustworthy edit source.
    receipt["stored_bytes"] = 999
    monkeypatch.setattr("orchestration.sdk.snapshot", lambda _: (plan, current))
    result = service.work(identity)
    assert "artifact" not in result["assets"][0]
