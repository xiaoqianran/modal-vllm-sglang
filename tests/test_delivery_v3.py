"""V3 invariants: isolated metadata, active index, immutable microbatch recovery."""
import asyncio
import hashlib
import json
import time
from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from runtime.config.storage import volume_key
from runtime.delivery import outbox, v3
from tests.test_durable_delivery import Ledger, SETTINGS, PNG, fixture, make_receipt


class Metadata(Ledger):
    def remove_file(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        del self.files[path]


@pytest.fixture
def worker_registry(monkeypatch):
    from threading import Lock
    from types import SimpleNamespace
    from pipeline import deploy_delivery_v3 as deployment
    rows = {}
    lock = Lock()
    async def put(key, value, *, skip_if_exists):
        with lock:
            if skip_if_exists and key in rows:
                return False
            rows[key] = value
            return True
    async def get(key):
        with lock:
            return rows.get(key)
    registry = SimpleNamespace(put=SimpleNamespace(aio=put), get=SimpleNamespace(aio=get))
    monkeypatch.setattr(deployment, "registry", registry)
    return registry


def setup(n=0, state="completed"):
    source, store = Ledger(), Metadata()
    # Reuse the generation fixture, but keep generation evidence only in source.
    intent, result = fixture(source, n, state)
    source.files.pop(volume_key("/" + outbox.path(intent["id"], "intent.json")))
    v3.register(store, intent)
    return source, store, intent, result


@pytest.mark.parametrize("derived", [False, True])
def test_missing_modal_index_is_empty(derived):
    volume = Mock()

    async def missing(key, recursive):
        raise v3.NotFoundError(f'path "{key}" does not exist')
        yield

    volume.iterdir.aio = missing
    assert list(v3.active_ids(v3.Ledger(volume), derived)) == []


def test_active_index_only_and_separate_storage():
    source, store, intent, _ = setup()
    assert list(v3.active_ids(store)) == [intent["id"]]
    assert outbox.read(source, outbox.path(intent["id"], "intent.json")) is None
    assert outbox.resolve(store, intent) is None
    i, request, n = v3.ready(store, source, intent["id"])
    assert i == intent and n == 0
    assert request["artifact"]["sha256"] == hashlib.sha256(PNG).hexdigest()
    for idx in range(1000):
        store.files[volume_key(f"/delivery-v2/00/history{idx}/receipt.json")] = b"{}"
    assert list(v3.active_ids(store)) == [intent["id"]]
    assert all("delivery-v3/active" in path for path, _ in store.lists)


def test_active_intent_recovers_missing_history():
    source, store, intent, _ = setup()
    store.remove_file(volume_key("/" + outbox.path(intent["id"], "intent.json")))
    assert v3.load(store, intent["id"]) == intent
    assert v3.ready(store, source, intent["id"])[0] == intent


def test_microbatch_conflict_reconciles_and_derivatives_survive():
    source, store, a, _ = setup()
    b, _ = fixture(source, 1)
    v3.register(store, b)
    ra, rb = [outbox.resolve(source, i, ledger=store) for i in (a, b)]
    rowa, rowb = make_receipt(ra), make_receipt(rb)
    outbox.save_receipt(store, a, ra, rowa)
    v3.save_receipts(store, [(a, ra, {**rowa, "reused": True}), (b, rb, rowb)])
    assert outbox.receipt(store, a, ra) == rowa
    assert outbox.receipt(store, b, rb) == rowb
    assert not list(v3.active_ids(store))
    assert set(v3.active_ids(store, True)) == {a["id"], b["id"]}
    v3.register(store, a)
    assert not list(v3.active_ids(store))


def test_ambiguous_success_is_reconciled():
    source, store, intent, _ = setup()
    req = outbox.resolve(source, intent)
    original = store.batch_upload
    calls = 0
    @contextmanager
    def ambiguous(force=False):
        nonlocal calls
        with original(force) as batch:
            yield batch
        calls += 1
        if calls == 1:
            raise TimeoutError("acknowledgement lost")
    store.batch_upload = ambiguous
    v3.save_receipts(store, [(intent, req, make_receipt(req))])
    assert outbox.receipt(store, intent, req)
    assert not list(v3.active_ids(store))


def test_receipt_only_crash_cleans_active_and_enqueues_derivative():
    source, store, intent, _ = setup()
    req = outbox.resolve(source, intent)
    outbox.save_receipt(store, intent, req, make_receipt(req))
    assert list(v3.active_ids(store)) == [intent["id"]]
    source.files.clear()
    assert v3.ready(store, source, intent["id"]) is None
    assert not list(v3.active_ids(store))
    assert list(v3.active_ids(store, True)) == [intent["id"]]


def test_claim_owner_and_expiry(monkeypatch):
    source, store, intent, _ = setup()
    assert v3.claim(store, intent, 0, "fc-owner")
    assert not v3.claim(store, intent, 0, "fc-other")
    assert v3.ready(store, source, intent["id"]) is None
    assert v3.ready(store, source, intent["id"], owner="fc-owner")[2] == 0
    monkeypatch.setattr(v3.time, "time", lambda: 10**12)
    assert v3.ready(store, source, intent["id"]) is None
    assert outbox.attempts(store, intent["id"])[0] == 1
    monkeypatch.setattr(v3.time, "time", lambda: 10**12+30)
    assert v3.ready(store, source, intent["id"])[2] == 1


def test_waiting_generation_remains_active():
    source, store, intent, _ = setup(state=None)
    assert v3.ready(store, source, intent["id"]) is None
    assert list(v3.active_ids(store)) == [intent["id"]]


def test_batch_outage_preserves_every_derivative_until_recovery(monkeypatch, worker_registry):
    from pipeline import deploy_delivery_v3 as deployment
    from runtime.archive import preview

    source, store, first, _ = setup()
    second, _ = fixture(source, 1, raw=PNG + b"b")
    v3.register(store, second)
    ids = [first["id"], second["id"]]
    for intent in (first, second):
        req = outbox.resolve(source, intent)
        v3.save_receipts(store, [(intent, req, make_receipt(req))])
    source.files.clear()
    monkeypatch.setenv("DELIVERY_V3_BUILD", v3.build_id())
    monkeypatch.setattr(deployment, "store", store)
    monkeypatch.setattr(preview, "backfill_batch", Mock(side_effect=TimeoutError))
    now = time.time()
    monkeypatch.setattr(v3.time, "time", lambda: now)
    for _ in range(3):
        assert deployment.derive_batch(ids)["processed"] == 0
        now += 3601
    for identity in ids:
        assert v3.derivative_attempt(store, identity)[0] == 3
        assert outbox.attempts(store, identity)[0] == 0
    assert set(v3.active_ids(store, True)) == set(ids)

    def recovered(bucket, receipts, **kwargs):
        return {r["url"]: {"source_sha256": r["stored_sha256"], "status": "original"} for r in receipts}

    monkeypatch.setattr(preview, "backfill_batch", recovered)
    assert deployment.derive_batch(ids)["processed"] == 2
    assert not list(v3.active_ids(store, True))


def test_build_includes_deployment_entrypoint(monkeypatch):
    from pathlib import Path
    original = Path.read_bytes
    before = v3.build_id()
    monkeypatch.setattr(Path, "read_bytes", lambda p: original(p) + (
        b"# deployment change" if p.name == "deploy_delivery_v3.py" else b""))
    assert v3.build_id() != before


def test_original_scan_resumes_after_removed_cursor(monkeypatch):
    from pipeline import deploy_delivery_v3 as deployment
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    ids = [f"{n:064x}" for n in range(36)]
    cursor = Mock()
    cursor.get.aio = AsyncMock(return_value=(8, ids[8]))
    cursor.put.aio = AsyncMock()
    monkeypatch.setattr(deployment.modal.Dict, "from_name", lambda *a, **kw: cursor)
    def indexed(store, *, derived, timestamps):
        timestamps.update({identity: n for n, identity in enumerate(ids) if n != 8})
        return iter(timestamps)
    monkeypatch.setattr(v3, "active_ids", indexed)
    monkeypatch.setattr(v3, "ready", lambda store, source, i, modal: (
        {"id": i}, {"artifact": {"size": 1}, "delivery": SETTINGS}, 0))
    monkeypatch.setattr(deployment, "dispatch_once", lambda *args: True)
    submitted = []
    monkeypatch.setattr(deployment, "dispatch", lambda worker, batch: submitted.extend(batch))
    summary = deployment.scan_originals()
    assert summary["indexed"] == 35 and summary["examined"] == 32
    assert submitted == ids[9:] + ids[:5]
    cursor.put.aio.assert_awaited_once_with("original", (4, ids[4]))


def test_derivative_scanner_reaches_item_49(monkeypatch):
    from pipeline import deploy_delivery_v3 as deployment
    from unittest.mock import AsyncMock
    ids = [f"{i:064x}" for i in range(49)]
    cursor = Mock()
    saved = {}
    cursor.get.aio = AsyncMock(side_effect=saved.get)
    cursor.put.aio = AsyncMock(side_effect=saved.__setitem__)
    monkeypatch.setattr(deployment.modal.Dict, "from_name", lambda *a, **kw: cursor)
    def indexed(store, *, derived, timestamps):
        assert derived
        timestamps.update(dict.fromkeys(ids, 100))
        return iter(ids)
    monkeypatch.setattr(v3, "active_ids", indexed)
    monkeypatch.setenv("DELIVERY_V3_BUILD", v3.build_id())
    monkeypatch.setattr(deployment, "dispatch_once", lambda *a, **kw: True)
    selected = []
    monkeypatch.setattr(v3, "derivative_due", lambda store, identity: 0)
    monkeypatch.setattr(deployment, "dispatch", lambda worker, batch, **kw: selected.extend(batch))
    for _ in range(4):
        assert deployment.scan_derivatives()["dispatched"] == 16
    assert selected[:49] == ids


def test_reservation_covers_worker_across_clock_boundary(monkeypatch, worker_registry):
    from concurrent.futures import ThreadPoolExecutor
    now = 10 * v3.LEASE - 1
    monkeypatch.setattr(v3.time, "time", lambda: now)
    def claim(_):
        return v3.reserve_worker(worker_registry, "derivative", "job")
    with ThreadPoolExecutor(8) as pool:
        assert sum(pool.map(claim, range(8))) == 1
    now += 2
    assert not claim(0)
    now += v3.LEASE
    assert claim(0)


def test_receipt_conflict_checks_winner_before_cleanup():
    source, store, intent, _ = setup()
    req = outbox.resolve(source, intent)
    row = make_receipt(req)
    identity = intent["id"]
    v3.save_receipts(store, [(intent, req, row)])
    key = outbox.path(identity, "derivative.json")
    result = {"status": "original", "source_sha256": row["stored_sha256"]}
    outbox.immutable(store, key, {**result, "source_sha256": "0"*64})
    with pytest.raises(ValueError, match="Invalid derivative source"):
        v3.finish_derivative(store, identity, row, result)
    assert list(v3.active_ids(store, True)) == [identity]
    store.remove_file(volume_key("/" + key))
    outbox.immutable(store, key, {**result, "status": "failed"})
    assert not v3.finish_derivative(store, identity, row, result)
    assert not list(v3.active_ids(store, True))


def test_buffer_does_not_wait_for_slow_file():
    source, store, intent, _ = setup()
    req = outbox.resolve(source, intent)
    b = v3.ReceiptBuffer(store, delay=.01)
    b.add((intent, req, make_receipt(req)))
    end = time.monotonic()+1
    while not outbox.read(store, outbox.path(intent["id"], "receipt.json")) and time.monotonic()<end:
        time.sleep(.01)
    assert outbox.receipt(store, intent, req) is not None
    b.close()


def test_deadline_cancels_await():
    async def slow():
        await asyncio.sleep(5)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        v3.deadline(slow(), .03)
    assert time.monotonic()-started < 1


def test_recovery_microbatches_respect_byte_and_count_limits():
    rows = [(dict(id=str(n)), {"artifact":{"size":size}, "delivery":SETTINGS}, 0)
            for n,size in enumerate([1, 12*1024*1024, outbox.MAX_BYTES+1] + [1]*9)]
    assert [[i["id"] for i,_,_ in g] for g in v3.groups(rows)] == [["0","1"],["2"],[str(i) for i in range(3,11)],["11"]]


def test_v3_plan_explicit_and_frozen(tmp_path, monkeypatch):
    from orchestration import state
    from pipeline.s01_build_plan import ROOT, build_plan
    import shutil
    shutil.copytree(ROOT/"workflows", tmp_path/"workflows")
    p = tmp_path/"tasks/099-durable.json"
    task = {"version":1,"name":"099-durable","workflow":"moss-t2i",
            "items":[{"id":"001","prompt":"mock"}],
            "delivery":{"provider":"huggingface","bucket":"test/isolated"}, "delivery_protocol":3}
    monkeypatch.setenv("DELIVERY_V3_VOLUME_NAME", "test-delivery-store")
    state.save(p,task)
    plan = build_plan(p,root=tmp_path)
    assert plan["delivery_protocol"] == 3
    assert plan["delivery_volume"] == "test-delivery-store"
    assert plan["delivery"]["compression"] == "none"
    assert plan["delivery"]["verify"] == "sha256"
    monkeypatch.setenv("DELIVERY_V3_VOLUME_NAME", plan["volume"])
    with pytest.raises(ValueError):
        build_plan(p,root=tmp_path)



def test_source_reads_metadata_with_async_deadline_but_media_stays_streamed():
    from types import SimpleNamespace
    class Reader:
        def __call__(self, key):
            assert key.endswith(".mp4")
            yield b"a"
            yield b"b"
        async def aio(self, key):
            assert key.endswith(".json")
            yield b"{}"
    view=v3.Source(SimpleNamespace(read_file=Reader()))
    assert list(view.read_file("/inference/result.json"))==[b"{}"]
    assert list(view.read_file("/inference/media.mp4"))==[b"a",b"b"]


def test_notify_coalesces_short_window(monkeypatch):
    import modal
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    spawn=AsyncMock()
    enqueue=AsyncMock()
    reserve=AsyncMock(side_effect=[True,False])
    from_name=Mock(return_value=SimpleNamespace(spawn=SimpleNamespace(aio=spawn)))
    monkeypatch.setattr(modal.Function,"from_name",from_name)
    monkeypatch.setattr(modal.Queue,"from_name",lambda *a,**kw: SimpleNamespace(put=SimpleNamespace(aio=enqueue)))
    monkeypatch.setattr(modal.Dict,"from_name",lambda *a,**kw: SimpleNamespace(put=SimpleNamespace(aio=reserve)))
    monkeypatch.setattr(v3.time,"time",lambda:100)
    v3.notify("a"*64,"isolated-app")
    v3.notify("b"*64,"isolated-app")
    assert enqueue.await_count == 2
    from_name.assert_called_once_with("isolated-app","archive_batch")
    spawn.assert_awaited_once_with([],3,event_partition="20")


def test_shard_and_legacy_marker_recovery():
    source, store, intent, _ = setup()
    identity = intent["id"]
    assert list(v3.active_ids(store, shard=identity[0])) == [identity]
    assert not list(v3.active_ids(store, shard="legacy"))
    outbox.immutable(store, v3.marker(identity, legacy=True), intent)
    store.remove_file(volume_key("/"+v3.marker(identity)))
    store.remove_file(volume_key("/"+outbox.path(identity,"intent.json")))
    assert list(v3.active_ids(store, shard="legacy")) == [identity]
    assert v3.load(store, identity) == intent
    req = outbox.resolve(source,intent)
    v3.save_receipts(store,[(intent,req,make_receipt(req))])
    assert not list(v3.active_ids(store))
    assert list(v3.active_ids(store,True,shard=identity[0])) == [identity]


def test_v3_backend_poll_uses_cached_metadata_not_media(monkeypatch):
    from orchestration.sdk import Backend
    from orchestration.sdk_session import SDKSession
    source,store,intent,_=setup()
    backend=Backend.__new__(Backend)
    backend.volume=source
    backend.modal=Mock()
    backend.session=SDKSession()
    factory=Mock(return_value=store)
    monkeypatch.setattr(v3,"ledger",factory)
    plan={"project":intent["project"],"delivery":intent["delivery"],"delivery_protocol":3,
          "delivery_volume":"isolated","delivery_app":"isolated-app"}
    job={"uses":"moss.t2i"}
    record={**intent["locator"],"delivery_id":intent["id"]}
    req=outbox.resolve(source,intent)
    record["artifacts"]={"image":req["artifact"]}
    outbox.save_receipt(store,intent,req,make_receipt(req))
    before=list(source.reads)
    assert backend.archive(None,job,record,plan)["derivative_status"]=="pending"
    assert backend.archive(None,job,record,plan)["stored_sha256"]==req["artifact"]["sha256"]
    assert source.reads==before
    factory.assert_called_once_with(backend.modal,plan)


def test_independent_verified_callback_before_slow_check(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from runtime.delivery.originals import ObservedBucket,publish_originals
    from runtime.archive.pipeline import asset_path
    source=Ledger()
    requests=[]
    for n in range(2):
        raw=PNG+bytes([n])
        intent,_=fixture(source,n,raw=raw)
        request=outbox.resolve(source,intent)
        path=tmp_path/request["artifact"]["path"]
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(raw)
        requests.append(request)
    release,fast=Event(),Event()
    slow_sha=requests[0]["artifact"]["sha256"]
    class Provider(ObservedBucket):
        def __init__(self,bucket):
            self.bucket=bucket
        def verify_digest(self,remote_path,size,expected):
            if expected==slow_sha:
                assert release.wait(2)
            return "sha256"
    def callback(i,row):
        if i==1:
            fast.set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(publish_originals,tmp_path,requests,provider_factory=Provider,on_receipt=callback)
        try:
            assert fast.wait(1)
            assert not future.done()
        finally:
            release.set()
        assert all("receipt" in row for row in future.result())



def test_verified_volume_snapshot_requires_exact_stored_bytes(tmp_path):
    from runtime.archive.operations import verified_volume_copy
    source, _, intent, _ = setup()
    artifact = outbox.resolve(source, intent)["artifact"]
    original = tmp_path / artifact["path"]
    original.parent.mkdir(parents=True)
    row = {"source_artifact": artifact, "stored_sha256": artifact["sha256"],
           "stored_bytes": len(PNG)}
    snapshot = tmp_path / "snapshot.png"
    assert not verified_volume_copy(tmp_path, row, snapshot)
    original.write_bytes(PNG)
    assert verified_volume_copy(tmp_path, row, snapshot)
    assert snapshot.read_bytes() == PNG
    original.write_bytes(PNG[:-1] + bytes([PNG[-1] ^ 1]))
    assert not verified_volume_copy(tmp_path, row, snapshot)
    assert not verified_volume_copy(tmp_path, {**row, "stored_sha256": "0"*64}, snapshot)


def test_derivative_dispatch_failure_preserves_receipt_and_recovery_marker(monkeypatch, worker_registry):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from pipeline import deploy_delivery_v3 as deployment
    from runtime.delivery import originals

    source, store, intent, _ = setup()
    identity = intent["id"]
    monkeypatch.setenv("DELIVERY_V3_BUILD", v3.build_id())
    monkeypatch.setattr(deployment, "source_records", source)
    monkeypatch.setattr(deployment, "store", store)
    monkeypatch.setattr(deployment, "volume", SimpleNamespace(reload=SimpleNamespace(aio=AsyncMock())))
    monkeypatch.setattr(deployment.modal, "current_function_call_id", lambda: "fc-owner")
    def publish(root, requests, *, metrics, on_receipt, **kwargs):
        receipt = make_receipt(requests[0])
        metrics["archived"] = 1
        on_receipt(0, receipt)
        return [{"receipt": receipt}]
    monkeypatch.setattr(originals, "publish_originals", publish)
    observed_receipts = []
    def crash(ids, protocol, **kwargs):
        observed_receipts.append(outbox.read(store, outbox.path(identity, "receipt.json")))
        raise RuntimeError("lost derivative worker")
    derivative = Mock(side_effect=crash)
    monkeypatch.setattr(deployment, "derive_batch", derivative)
    assert deployment.archive_batch.local([identity])["archived"] == 1
    derivative.assert_called_once_with([identity], v3.PROTOCOL, local_inputs={}, owner="fc-owner")
    assert observed_receipts[0]["status"] == "archived"
    assert list(v3.active_ids(store, True)) == [identity]
    assert not list(v3.active_ids(store))

def test_retained_original_is_private_and_survives_publication(tmp_path):
    from runtime.delivery.originals import ObservedBucket, publish_originals
    source = Ledger()
    intent, _ = fixture(source, 0)
    request = outbox.resolve(source, intent)
    original = tmp_path / request["artifact"]["path"]
    original.parent.mkdir(parents=True)
    original.write_bytes(PNG)
    class Provider(ObservedBucket):
        def __init__(self, bucket):
            self.bucket = bucket
        def verify_digest(self, remote_path, size, expected):
            return "sha256"
    retained = {}
    workspace = tmp_path / "private"
    workspace.mkdir()
    rows = publish_originals(tmp_path, [request], provider_factory=Provider,
                             workspace=workspace, local_inputs=retained)
    row = rows[0]["receipt"]
    local = retained[(row["bucket"], row["stored_sha256"], row["stored_bytes"])]
    assert local.is_relative_to(workspace)
    assert local != original and local.read_bytes() == PNG
    original.unlink()
    assert local.read_bytes() == PNG

@pytest.mark.parametrize("stage", ["original", "derivative"])
def test_terminal_owner_takeover_is_atomic(monkeypatch, worker_registry, stage):
    from concurrent.futures import ThreadPoolExecutor
    now = 10 * v3.LEASE - 1
    monkeypatch.setattr(v3.time, "time", lambda: now)
    assert v3.reserve_worker(worker_registry, stage, "job", owner="fc-old")
    assert not v3.reserve_worker(worker_registry, stage, "job", owner="fc-new", ended=lambda _: False)
    # Cross the slot boundary while the old reservation is still present.
    now += 2
    def compete(i):
        return v3.reserve_worker(worker_registry, stage, "job", owner=f"fc-{i}",
                                 ended=lambda call: call == "fc-old")
    with ThreadPoolExecutor(8) as pool:
        assert sum(pool.map(compete, range(8))) == 1


def test_legacy_reservation_cannot_be_taken_over(monkeypatch, worker_registry):
    monkeypatch.setattr(v3.time, "time", lambda: 100)
    assert v3.reserve_worker(worker_registry, "original", "job")
    assert not v3.reserve_worker(worker_registry, "original", "job", owner="fc-new", ended=lambda _: True)


def test_partial_reservation_recovered_after_owner_ends(monkeypatch, worker_registry):
    now = 100
    monkeypatch.setattr(v3.time, "time", lambda: now)
    import asyncio
    asyncio.run(worker_registry.put.aio(("original", "job", 0, 0),
                  {"call_id": "fc-crashed", "reserved_at": now}, skip_if_exists=True))
    assert not v3.reserve_worker(worker_registry, "original", "job", owner="fc-new", ended=lambda _: False)
    assert v3.reserve_worker(worker_registry, "original", "job", owner="fc-new",
                             ended=lambda call: call == "fc-crashed")

def test_derivative_retry_wait_and_exhaustion(monkeypatch):
    source, store, intent, _ = setup()
    identity = intent["id"]
    req = outbox.resolve(source, intent)
    row = make_receipt(req)
    v3.save_receipts(store, [(intent, req, row)])
    now = 1000
    monkeypatch.setattr(v3.time, "time", lambda: now)
    for n in range(outbox.MAX_ATTEMPTS):
        assert v3.derivative_due(store, identity) == n
        failure = v3.derivative_failure(store, identity, n, TimeoutError())
        assert v3.derivative_failure(store, identity, n, TimeoutError()) == failure
        assert outbox.attempts(store, identity)[0] == 0
        if n + 1 < outbox.MAX_ATTEMPTS:
            assert v3.derivative_due(store, identity) is None
        now = failure["retry_at"] + 1
    n, last = v3.derivative_attempt(store, identity)
    assert v3.reconcile_derivative_failure(store, identity, row, n, last)
    assert not list(v3.active_ids(store, True))
    assert outbox.read(store, outbox.path(identity, "derivative.json"))["status"] == "failed"
    assert outbox.receipt(store, intent, req) == row

def test_coordination_gc_only_expired_known_keys(monkeypatch):
    from types import SimpleNamespace
    now = 200000
    monkeypatch.setattr(v3.time, "time", lambda: now)
    identity = "a"*64
    expired = [("original", identity, 0, 1), ("derivative", identity, 2, 1, 3),
               ("dispatch", True, identity, 0, 1), ("event", 1)]
    protected = [("original", identity, 0, int(now//v3.LEASE)),
                 ("derivative", identity, 0, int(now//v3.LEASE)+1),
                 ("event", int(now//v3.EVENT_WINDOW)), ("unknown", 1),
                 ("original", "bad", 0, 1), ("dispatch", 1, identity, 0, 2)]
    rows = dict.fromkeys(expired + protected, True)
    async def keys():
        for key in list(rows):
            yield key
    async def pop(key, default):
        return rows.pop(key, default)
    registry = SimpleNamespace(keys=SimpleNamespace(aio=keys), pop=SimpleNamespace(aio=pop))
    assert v3.collect_coordination(registry, delete_limit=2)["removed"] == 2
    assert v3.collect_coordination(registry)["removed"] == 2
    assert set(rows) == set(protected)


def test_shard_budget_shared_even_when_nonempty(monkeypatch):
    from pipeline import deploy_delivery_v3 as deployment
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    cursor = SimpleNamespace(get=SimpleNamespace(aio=AsyncMock(return_value=0)),
                             put=SimpleNamespace(aio=AsyncMock()))
    monkeypatch.setattr(deployment.modal.Dict, "from_name", lambda *a, **kw: cursor)
    calls = []
    def scan(shard, *, end, limit, check_limit):
        calls.append((shard, limit, check_limit))
        return {"indexed": 100, "examined": check_limit, "dispatched": limit}
    result = deployment.scan_sharded("original", scan)
    assert [c[0] for c in calls] == ["legacy", "0", "1", "2"]
    assert sum(c[1] for c in calls) == 32
    assert sum(c[2] for c in calls) == 128
    assert result["dispatched"] == 32


def test_event_partition_waits_and_checks_once(monkeypatch):
    from pipeline import deploy_delivery_v3 as deployment
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    now = 102
    monkeypatch.setattr(deployment.time, "time", lambda: now)
    slept = []
    monkeypatch.setattr(deployment.time, "sleep", slept.append)
    monkeypatch.setattr(deployment.modal, "current_function_call_id", lambda: "fc-event")
    queue = SimpleNamespace(get_many=SimpleNamespace(aio=AsyncMock(return_value=["a","a","b"])))
    monkeypatch.setattr(deployment.modal.Queue, "from_name", lambda *a, **kw: queue)
    ready = Mock(side_effect=lambda identity, owner: (
        {"id":identity}, {"artifact":{"size":1},"delivery":SETTINGS}, 0))
    monkeypatch.setattr(deployment, "prepare_original", ready)
    archive = Mock(side_effect=lambda *a, **kw: {"archived":len(kw["prepared"])})
    monkeypatch.setattr(deployment, "archive_originals", archive)
    result = deployment.process_event_partition("20", v3.PROTOCOL, time.monotonic())
    assert slept == [4]
    assert ready.call_count == 2
    assert archive.call_count == 1
    assert result == {"event_hints":3,"archived":2}

def test_coordination_gc_bounds_attempts_even_if_keys_disappear(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(v3.time, "time", lambda: 200000)
    async def keys():
        for i in range(1,100):
            yield ("event",i)
    async def pop(key, default):
        return None
    registry = SimpleNamespace(keys=SimpleNamespace(aio=keys), pop=SimpleNamespace(aio=pop))
    result = v3.collect_coordination(registry, delete_limit=5)
    assert result["attempted"] == 5
    assert result["removed"] == 0
