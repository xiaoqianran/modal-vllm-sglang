"""Protocol 3 coordination over a separate, SDK-only metadata Volume V2."""
from contextlib import contextmanager
from contextvars import ContextVar
import asyncio
import io
import json
import os
import re
import time
from queue import Queue, Empty
from threading import Thread, Lock
from collections import OrderedDict
from modal.exception import NotFoundError

from runtime.config.storage import volume_key
from runtime.delivery import outbox
from runtime.utils.volume_records import put_once

PROTOCOL = 3
APP = "modal-vllm-sglang-delivery-v3"
VOLUME = "delivery-store"
ACTIVE = "delivery-v3/active"
DERIVED = "delivery-v3/derivatives"
EVENT_WINDOW = 5
LEASE = 660  # Longer than the worker's hard execution deadline.
ID = re.compile(r"[0-9a-f]{64}")
MAX_RECORD = 1024 * 1024
_metadata_ops = ContextVar("delivery_metadata_ops", default=None)


@contextmanager
def count_metadata():
    counts = {}
    token = _metadata_ops.set(counts)
    try:
        yield counts
    finally:
        _metadata_ops.reset(token)


def _count(operation):
    counts = _metadata_ops.get()
    if counts is not None:
        counts[operation] = counts.get(operation, 0) + 1


def app_name():
    return os.getenv("DELIVERY_V3_APP_NAME", APP)


def volume_name():
    return os.getenv("DELIVERY_V3_VOLUME_NAME", VOLUME)


def deadline(coro, seconds=15):
    """Use the public async SDK, so a timeout cancels the local await."""
    async def run():
        async with asyncio.timeout(seconds):
            return await coro
    return asyncio.run(run())


class Ledger:
    """Bound metadata RPCs, without mounting the metadata filesystem."""
    def __init__(self, volume):
        self.volume = volume
        self._immutable = OrderedDict()
        self._cache_lock = Lock()

    def read_file(self, key):
        immutable = "/delivery-v2/" in "/" + key.lstrip("/") and key.endswith(
            ("/intent.json", "/receipt.json", "/derivative.json", "/terminal.json", "/blocked.json"))
        if immutable:
            with self._cache_lock:
                cached = self._immutable.get(key)
                if cached is not None:
                    self._immutable.move_to_end(key)
            if cached is not None:
                _count("cache_hit")
                yield cached
                return
        _count("read")
        async def read():
            data = bytearray()
            async for chunk in self.volume.read_file.aio(key):
                data.extend(chunk)
                if len(data) > MAX_RECORD:
                    raise ValueError("Delivery metadata too large")
            return bytes(data)
        data = deadline(read(), 10)
        if immutable and len(data) <= 16384:
            with self._cache_lock:
                self._immutable[key] = data
                self._immutable.move_to_end(key)
                while len(self._immutable) > 512:
                    self._immutable.popitem(last=False)
        yield data

    @contextmanager
    def batch_upload(self, force=False):
        if force:
            raise ValueError("Delivery records are immutable")
        rows = []
        class Batch:
            def put_file(self, stream, key):
                rows.append((stream.read(), key))
        yield Batch()
        _count("upload")
        async def upload():
            async with self.volume.batch_upload.aio(force=False) as batch:
                for data, key in rows:
                    batch.put_file(io.BytesIO(data), key)
        deadline(upload())

    def iterdir(self, key, recursive=True):
        _count("list")
        async def ls():
            return [entry async for entry in self.volume.iterdir.aio(key, recursive=recursive)]
        try:
            return iter(deadline(ls()))
        except NotFoundError as exc:
            if str(exc) == f'path "{key}" does not exist':
                raise FileNotFoundError(key) from exc
            raise

    def remove_file(self, key):
        _count("remove")
        deadline(self.volume.remove_file.aio(key))


class Source(Ledger):
    """Bound generation metadata reads; preserve streaming for actual media."""
    def read_file(self, key):
        if key.endswith(".json"):
            yield from super().read_file(key)
        else:
            yield from self.volume.read_file(key)


def ledger(modal, plan=None):
    name = (plan or {}).get("delivery_volume", volume_name())
    return Ledger(modal.Volume.from_name(name, create_if_missing=False, version=2))


def marker(identity, derived=False, *, legacy=False):
    if not ID.fullmatch(identity):
        raise ValueError("Invalid delivery ID")
    root = DERIVED if derived else ACTIVE
    return f"{root}/{identity}.json" if legacy else f"{root}/shards/{identity[0]}/{identity}.json"


def remove(store, key):
    try:
        store.remove_file(volume_key("/" + key))
    except FileNotFoundError:
        pass


def remove_marker(store, identity, derived=False):
    remove(store, marker(identity, derived))
    remove(store, marker(identity, derived, legacy=True))


def register(store, intent):
    """Active intent is complete, not a pointer: recover a lost history write."""
    identity = intent["id"]
    if not any(outbox.read(store, outbox.path(identity, k)) is not None
               for k in ("receipt.json", "terminal.json", "blocked.json")):
        outbox.immutable(store, marker(identity), intent)
    outbox.register(store, intent)
    return intent


def active_ids(store, derived=False, *, timestamps=None, shard=None):
    """Enumerate one durable shard (0..f), legacy flat markers, or all for audit."""
    root = DERIVED if derived else ACTIVE
    if shard is None:
        locations = [(root, True)] + [(f"{root}/shards/{c}", False) for c in "0123456789abcdef"]
    elif shard == "legacy":
        locations = [(root, True)]
    elif shard in "0123456789abcdef" and len(shard) == 1:
        locations = [(f"{root}/shards/{shard}", False)]
    else:
        raise ValueError("Invalid delivery shard")
    rows = {}
    for location, legacy in locations:
        try:
            for e in store.iterdir(volume_key("/" + location), recursive=False):
                path = e.path.replace("\\", "/").strip("/")
                name = path.rsplit("/", 1)[-1]
                if not name.endswith(".json") or not ID.fullmatch(name[:-5]):
                    continue
                # Do not treat recursively returned nested records as legacy entries.
                if not path.endswith(f"{location}/{name}"):
                    continue
                identity = name[:-5]
                stamp = getattr(e, "mtime", 0)
                rows[identity] = min(rows.get(identity, stamp), stamp)
        except FileNotFoundError:
            continue
    for identity in sorted(rows, key=lambda i: (rows[i], i)):
        if timestamps is not None:
            timestamps[identity] = rows[identity]
        yield identity


def load(store, identity):
    intent = outbox.read(store, outbox.path(identity, "intent.json"))
    if intent is None:
        active = outbox.read(store, marker(identity))
        if active is None:
            active = outbox.read(store, marker(identity, legacy=True))
        if not active or active.get("id") != identity:
            raise ValueError("Missing delivery intent")
        outbox.register(store, active)
        return outbox.load_intent(store, identity)
    return outbox.validate_intent(intent, identity)


def finish(store, intent, row):
    # Recovery must be able to enqueue derivatives even after a receipt-only crash.
    identity = intent["id"]
    if outbox.read(store, outbox.path(identity, "derivative.json")) is None:
        outbox.immutable(store, marker(identity, True), {"source_sha256": row["stored_sha256"]})
    remove_marker(store, identity)


def committed_receipt(intent, row):
    """Receipts live under the intent ID and were verified before publication."""
    from runtime.tasks.result_integrity import validate_media_record
    loc = intent["locator"]
    artifact = row["source_artifact"]
    directory, _, filename = artifact["path"].rpartition("/")
    if directory != loc["prefix"] or artifact["type"] != intent["kind"] or artifact["storage"] != "volume":
        raise ValueError("Receipt source differs from delivery intent")
    validate_media_record(loc, {"id": loc["id"], "file": filename, "bytes": artifact["size"],
                                "sha256": artifact["sha256"]})
    request = {"artifact": artifact, "delivery": intent["delivery"]}
    if "work" in intent:
        request["work"] = intent["work"]
    return outbox.validate_receipt(row, request)


def ready(store, source, identity, modal=None, *, owner=None):
    intent = load(store, identity)
    completed = outbox.read(store, outbox.path(identity, "receipt.json"))
    if completed is not None:
        finish(store, intent, committed_receipt(intent, completed))
        return None
    if any(outbox.read(store, outbox.path(identity, k)) is not None
           for k in ("blocked.json", "terminal.json")):
        remove_marker(store, identity)
        return None
    n, last = outbox.attempts(store, identity)
    if n >= outbox.MAX_ATTEMPTS or last and last["kind"] != "retry":
        if last:
            put_once(store, outbox.path(identity, "blocked.json"), last)
        remove_marker(store, identity)
        return None
    if last and time.time() < last["retry_at"]:
        return None
    claim = outbox.read(store, outbox.path(identity, f"claim-{n}.json"))
    if claim and claim["call_id"] != owner:
        # The lease suppresses duplicate work, never establishes correctness.
        # A positive terminal observation can accelerate recovery; absence
        # or an RPC failure is not terminal evidence.
        if time.time() < claim["expires_at"] and not terminal_hint(modal, claim["call_id"]):
            return None
        outbox.failure(store, intent, n, RuntimeError("Delivery owner ended or lease expired"))
        return None
    try:
        request = outbox.resolve(source, intent, modal, ledger=store, terminal_lookup=terminal_status)
        if request and not request.get("terminal") and "work" in intent:
            from runtime.archive.works import task_path, validate_task
            work = intent["work"]
            task = outbox.read(store, task_path(work["id"]))
            if task is None:
                # A delayed definition is retriable, not permission to publish
                # incomplete metadata or retry GPU inference.
                raise FileNotFoundError("Missing work task definition")
            task = validate_task(work["id"], task)
            if not any(j["item"] == work["item"] and j["step"] == work["step"] for j in task["jobs"]):
                raise ValueError("Delivery step is absent from the frozen work definition")
            request["work_task"] = task
    except Exception as exc:
        outbox.failure(store, intent, n, exc)
        return None
    if request and request.get("terminal"):
        put_once(store, outbox.path(identity, "terminal.json"), {"state": "generation_failed"})
        remove_marker(store, identity)
    elif request:
        return intent, request, n


def terminal_status(modal, call_id):
    if modal is None or not isinstance(call_id, str) or not call_id.startswith("fc-"):
        return None
    try:
        from modal.types import InputStatus
        from runtime.tasks.task_lifecycle import CALL_TERMINAL
        graph = deadline(modal.FunctionCall.from_id(call_id).get_call_graph.aio(), 3)
        nodes = [x for x in graph if x.function_call_id == call_id]
        if (len(nodes) == 1 and isinstance(nodes[0].status, InputStatus)
                and nodes[0].status.name in CALL_TERMINAL):
            return nodes[0].status.name
    except Exception:
        pass
    return None


def terminal_hint(modal, call_id):
    return terminal_status(modal, call_id) is not None


def claim(store, intent, n, call_id):
    return outbox.claim(store, intent, n, call_id, lease_seconds=LEASE)


def reserve_worker(registry, stage, identity, attempt=0, *, owner=None, ended=None):
    """Append-only slot generations; never delete a live or ambiguous reservation.

    Both adjacent slots must be acquired before execution. A predecessor is
    replaceable only with positive platform terminal evidence. Legacy boolean
    reservations remain opaque until their original time window expires.
    """
    if owner is not None and (not isinstance(owner, str) or not owner.startswith("fc-")):
        raise ValueError("Invalid delivery owner")
    slot = int(time.time() // LEASE)
    record = True if owner is None else {"call_id": owner, "reserved_at": time.time()}
    for window in (slot, slot + 1):
        base = (stage, identity, attempt, window)
        for generation in range(8):
            key = base if generation == 0 else (*base, generation)
            if deadline(registry.put.aio(key, record, skip_if_exists=True), 5):
                break
            if owner is None or ended is None:
                return False
            previous = deadline(registry.get.aio(key), 5)
            if not isinstance(previous, dict):
                return False
            predecessor = previous.get("call_id")
            if not isinstance(predecessor, str) or predecessor == owner or not ended(predecessor):
                return False
        else:
            return False
    return int(time.time() // LEASE) == slot


def coordination_expired(key, now, *, retention=86400):
    """Only known time-bucket keys, long after their final possible use."""
    if not isinstance(key, tuple) or not key:
        return False
    kind = key[0]
    if kind == "event" and len(key) == 2:
        epoch, window = key[1], EVENT_WINDOW
    elif kind == "dispatch" and len(key) == 5:
        _, derived, identity, attempt, epoch = key
        if type(derived) is not bool or not isinstance(identity, str) or not ID.fullmatch(identity):
            return False
        if type(attempt) is not int or attempt < 0:
            return False
        window = 360 if derived else 120
    elif kind in ("original", "derivative") and len(key) in (4, 5):
        _, identity, attempt, epoch, *generation = key
        if not isinstance(identity, str) or not ID.fullmatch(identity):
            return False
        if type(attempt) is not int or attempt < 0:
            return False
        if generation and (type(generation[0]) is not int or not 1 <= generation[0] < 8):
            return False
        window = LEASE
    else:
        return False
    return type(epoch) is int and epoch >= 0 and (epoch + 1) * window + max(retention, 2 * LEASE) <= now


def collect_coordination(registry, *, inspect_limit=4096, delete_limit=256, seconds=8):
    """Bound client-side enumeration and deletion; never touch durable receipts."""
    async def collect():
        examined = removed = errors = attempted = 0
        now = time.time()
        end = time.monotonic() + seconds
        stream = registry.keys.aio()
        batch = []

        async def flush():
            nonlocal removed, errors, attempted
            if not batch:
                return
            # Every key has already passed the conservative expiry predicate.
            # Bound parallel requests and do not retry ambiguous deletions here.
            async def discard(key):
                try:
                    async with asyncio.timeout(max(.05, end-time.monotonic())):
                        return await registry.pop.aio(key, None) is not None
                except Exception:
                    return None
            attempted += len(batch)
            results = await asyncio.gather(*(discard(key) for key in batch))
            removed += sum(r is True for r in results)
            errors += sum(r is None for r in results)
            batch.clear()

        try:
            async for key in stream:
                examined += 1
                if coordination_expired(key, now):
                    batch.append(key)
                if len(batch) >= 16 or attempted + len(batch) >= delete_limit:
                    await flush()
                if examined >= inspect_limit or attempted >= delete_limit or time.monotonic() >= end:
                    break
            if batch and time.monotonic() < end:
                await flush()
        finally:
            await stream.aclose()
        return {"examined": examined, "attempted": attempted, "removed": removed, "errors": errors}
    return deadline(collect(), seconds + 2)


def terminal_cache(modal):
    """One terminal observation per previous batch owner, not per asset."""
    observed = {}

    def ended(call_id):
        if call_id not in observed:
            observed[call_id] = terminal_hint(modal, call_id)
        return observed[call_id]

    return ended


def derivative_attempt(store, identity):
    """Independent append-only retry history; never consumes original attempts."""
    last = None
    for n in range(outbox.MAX_ATTEMPTS):
        row = outbox.read(store, outbox.path(identity, f"derive-failure-{n}.json"))
        if row is None:
            return n, last
        last = row
        if row["kind"] != "retry":
            return n + 1, last
    return outbox.MAX_ATTEMPTS, last


def derivative_due(store, identity):
    n, last = derivative_attempt(store, identity)
    if last and (n >= outbox.MAX_ATTEMPTS or last["kind"] != "retry"):
        # A worker must reconcile the terminal receipt before removing its marker.
        return n
    if last and time.time() < last["retry_at"]:
        return None
    return n


def derivative_failure(store, identity, n, exc):
    if n >= outbox.MAX_ATTEMPTS:
        raise ValueError("Derivative retry budget exhausted")
    if isinstance(exc, dict):
        detail = exc
    else:
        detail = {"kind": outbox.classify(exc), "error": type(exc).__name__,
                  "retry_after": outbox.retry_after(exc)}
    row = {"kind": detail["kind"], "error": str(detail["error"])[:512],
           "retry_at": time.time() + max(detail.get("retry_after", 0), min(900, 15 * 2**n))}
    key = outbox.path(identity, f"derive-failure-{n}.json")
    if not put_once(store, key, row):
        row = outbox.read(store, key)
    return row


def derivative_exhausted(n, last):
    return last is not None and (n >= outbox.MAX_ATTEMPTS or last["kind"] != "retry")


def reconcile_derivative_failure(store, identity, original, n, last):
    if not derivative_exhausted(n, last):
        return False
    finish_derivative(store, identity, original,
                      {"status": "failed", "error": last["error"],
                       "source_sha256": original["stored_sha256"],
                       "attempts": n, "failure_kind": last["kind"]})
    return True


def finish_derivative(store, identity, original, result):
    outbox.validate_derivative(result, original)
    key = outbox.path(identity, "derivative.json")
    created = put_once(store, key, result)
    if not created:
        winner = outbox.read(store, key)
        outbox.validate_derivative(winner, original)
        if result["status"] != winner["status"]:
            print("DERIVATIVE_RESULT_CONFLICT", identity, result["status"], winner["status"], flush=True)
    remove_marker(store, identity, True)
    return created


def groups(rows):
    """Bound each bucket's recovery batches by both upload bytes and count."""
    pending = {}
    for row in rows:
        request = row[1]
        key = request["delivery"]["bucket"]
        size = request["artifact"]["size"]
        items, used = pending.pop(key, ([], 0))
        if items and used + size > outbox.MAX_BYTES:
            yield items
            items, used = [], 0
        items.append(row)
        used += size
        if len(items) >= outbox.MAX_FILES or used >= outbox.MAX_BYTES:
            yield items
        else:
            pending[key] = items, used
    for items, _ in pending.values():
        yield items


def save_receipts(store, rows):
    """Never overwrite. Reconcile ambiguous or conflicting batch submissions."""
    pending = {}
    for intent, request, row in rows:
        outbox.validate_receipt(row, request)
        pending[outbox.path(intent["id"], "receipt.json")] = (intent, request, row)
    confirmed = {}
    for attempt in range(2):
        if not pending:
            break
        try:
            with store.batch_upload(force=False) as batch:
                for key, (_, _, row) in pending.items():
                    batch.put_file(io.BytesIO(json.dumps(row, sort_keys=True, allow_nan=False).encode()),
                                   volume_key("/" + key))
        except Exception:
            # On a conflict or timeout, the remote operation may have succeeded.
            missing = {}
            for key, (intent, request, row) in pending.items():
                existing = outbox.read(store, key)
                if existing is None:
                    missing[key] = (intent, request, row)
                else:
                    confirmed[key] = outbox.validate_receipt(existing, request)
            if attempt and missing:
                raise
            pending = missing
        else:
            confirmed.update({key: row for key, (_, _, row) in pending.items()})
            pending = {}
    for intent, request, _ in rows:
        key = outbox.path(intent["id"], "receipt.json")
        if key not in confirmed:
            raise RuntimeError("Receipt publication is not confirmed")
        finish(store, intent, confirmed[key])


class ReceiptBuffer:
    """Flush ready receipts without waiting for an unfinished media verification."""
    def __init__(self, store, *, delay=.05, count=4):
        self.store, self.delay, self.count = store, delay, count
        self.queue = Queue()
        self.error = None
        self.thread = Thread(target=self._run, daemon=True)
        self.thread.start()

    def add(self, row):
        self.queue.put(row)

    def _run(self):
        try:
            while True:
                item = self.queue.get()
                if item is None:
                    return
                batch = [item]
                end = time.monotonic() + self.delay
                while len(batch) < self.count:
                    try:
                        item = self.queue.get(timeout=max(0, end - time.monotonic()))
                    except Empty:
                        break
                    if item is None:
                        break
                    batch.append(item)
                save_receipts(self.store, batch)
                if item is None:
                    return
        except Exception as exc:
            self.error = exc

    def close(self):
        self.queue.put(None)
        self.thread.join()
        if self.error is not None:
            raise self.error


def notify(identity, app=None, *, direct=False):
    """Wake a shared worker; on ambiguous dispatch, prefer a safe duplicate."""
    import modal
    if not ID.fullmatch(identity):
        raise ValueError("Invalid delivery identity")
    name = app or app_name()

    async def spawn():
        epoch = int(time.time() // EVENT_WINDOW)
        partition = str(epoch)
        queue = modal.Queue.from_name(name + "-events", create_if_missing=True)
        registry = modal.Dict.from_name(name + "-workers", create_if_missing=True)
        await queue.put.aio(identity, partition=partition, partition_ttl=120)
        if await registry.put.aio(("event", epoch), True, skip_if_exists=True):
            await modal.Function.from_name(name, "archive_batch").spawn.aio([], PROTOCOL, event_partition=partition)

    async def dispatch():
        await modal.Function.from_name(name, "archive_batch").spawn.aio([identity], PROTOCOL)

    if not direct:
        try:
            deadline(spawn(), 8)
            return True
        except Exception as exc:
            # A timeout may happen after a reservation but before spawn.
            # The durable claim prevents duplicate upload on ambiguous success.
            print("DELIVERY_EVENT_FALLBACK", type(exc).__name__, flush=True)
    try:
        deadline(dispatch(), 10)
        return True
    except Exception as exc:
        print("DELIVERY_EVENT_DEFERRED", type(exc).__name__, flush=True)
        return False


def build_id():
    from hashlib import sha256
    from pathlib import Path
    root = Path(__file__).resolve()
    digest = sha256(outbox.build_id().encode())
    for path in (root, root.parents[2] / "pipeline/deploy_delivery_v3.py"):
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def assert_build(protocol=PROTOCOL):
    actual = build_id()
    if protocol != PROTOCOL or actual != os.getenv("DELIVERY_V3_BUILD"):
        raise RuntimeError("V3 delivery protocol/build mismatch")
    return actual
