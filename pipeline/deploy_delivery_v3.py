"""Deploy protocol 3 independently; generation and legacy apps are untouched."""
import os
import math
import time
from bisect import bisect_right
import modal

from concurrent.futures import ThreadPoolExecutor
from tempfile import TemporaryDirectory

from runtime.config.storage import VOLUME_MOUNT, VOLUME_SUB_PATH, runtime_volume_name
from runtime.delivery import outbox, v3

app = modal.App(v3.app_name())
scheduled = os.getenv("DELIVERY_V3_SCHEDULE", "1") == "1"
# Anonymous test volumes are accepted only by an explicitly isolated lab App.
lab_ids = (os.getenv("DELIVERY_V3_LAB_SOURCE_ID"), os.getenv("DELIVERY_V3_LAB_LEDGER_ID"))
if any(lab_ids) and (not all(lab_ids) or scheduled or not v3.app_name().startswith("lab-delivery-v3-")):
    raise ValueError("Anonymous delivery storage is restricted to unscheduled lab Apps")
volume = (modal.Volume.from_id(lab_ids[0]).hydrate() if lab_ids[0] else
          modal.Volume.from_name(runtime_volume_name(), create_if_missing=False, version=2))
metadata = (modal.Volume.from_id(lab_ids[1]).hydrate() if lab_ids[1] else
            modal.Volume.from_name(v3.volume_name(), create_if_missing=False, version=2))
if not lab_ids[0] and runtime_volume_name() == v3.volume_name():
    raise ValueError("V3 metadata must use a separate Volume")
if lab_ids[0] and lab_ids[0] == lab_ids[1]:
    raise ValueError("V3 metadata must use a separate Volume")
store = v3.Ledger(metadata)
lab_registry_id = os.getenv("DELIVERY_V3_LAB_REGISTRY_ID")
if lab_registry_id and not all(lab_ids):
    raise ValueError("Anonymous worker registry requires isolated lab volumes")
registry = (modal.Dict.from_id(lab_registry_id) if lab_registry_id else
            modal.Dict.from_name(v3.app_name() + "-workers", create_if_missing=True))
source_records = v3.Source(volume)
source_mount = volume.with_mount_options(read_only=True, sub_path=VOLUME_SUB_PATH)
secret = modal.Secret.from_name("huggingface-secret", required_keys=["HF_TOKEN"])
region = os.getenv("DELIVERY_V3_REGION", "auto")
# One pool, including during recovery: do not create independent with_options pools.
region_options = {} if region == "auto" else {"region": region}
env = {"DELIVERY_V3_BUILD": v3.build_id(), "DELIVERY_V3_APP_NAME": v3.app_name(),
       "DELIVERY_V3_VOLUME_NAME": v3.volume_name(), "MODEL_VOLUME_NAME": runtime_volume_name(),
       "DELIVERY_V3_REGION": region, "DELIVERY_V3_SCHEDULE": "1" if scheduled else "0"}
if lab_ids[0]:
    env.update(DELIVERY_V3_LAB_SOURCE_ID=lab_ids[0], DELIVERY_V3_LAB_LEDGER_ID=lab_ids[1])
if lab_registry_id:
    env["DELIVERY_V3_LAB_REGISTRY_ID"] = lab_registry_id
base_image = (modal.Image.debian_slim(python_version="3.12")
         .uv_pip_install("huggingface-hub>=2.4,<3", "pillow>=13", "python-dotenv>=1",
                         "modal>=1.6.3,<2", index_url="https://pypi.org/simple"))


def with_source(base):
    return base.env(env).add_local_python_source("runtime", "orchestration", "pipeline", copy=True)


image = with_source(base_image)
scanner = dict(image=image, cpu=.25, memory=512, timeout=120, max_containers=1,
               scaledown_window=2, **region_options)


@app.function(image=image, cpu=.25, memory=512, timeout=60, max_containers=1, secrets=[secret])
def check_bucket(bucket, protocol):
    from huggingface_hub import HfApi
    from runtime.archive.huggingface import public_url
    build = v3.assert_build(protocol)
    public_url(bucket, "readiness")
    api = HfApi(token=os.environ["HF_TOKEN"])
    if api.whoami().get("name") != bucket.split("/", 1)[0] or getattr(api.bucket_info(bucket), "private", False):
        raise ValueError("HF bucket must be public and owned by the configured secret")
    volume.hydrate()
    metadata.hydrate()
    if volume.object_id == metadata.object_id:
        raise ValueError("Media and metadata storage must be distinct")
    return {"ready": True, "protocol": v3.PROTOCOL, "build": build,
            "volume_id": volume.object_id, "delivery_volume_id": metadata.object_id}


media_image = with_source(base_image.apt_install("ffmpeg"))


media_scaledown = int(os.getenv("DELIVERY_V3_MEDIA_SCALEDOWN", "60"))
if media_scaledown not in (10, 20, 30, 60):
    raise ValueError("DELIVERY_V3_MEDIA_SCALEDOWN must be 10, 20, 30 or 60")
media_worker = dict(image=media_image, cpu=1, memory=2048, timeout=600,
                    scaledown_window=media_scaledown, volumes={VOLUME_MOUNT: source_mount},
                    secrets=[secret], **region_options)


@app.function(**media_worker, max_containers=4)
def archive_batch(identities, protocol=v3.PROTOCOL, *, derivative=False, event_partition=None):
    v3.assert_build(protocol)
    started = time.monotonic()
    if event_partition is not None:
        if derivative or identities or not isinstance(event_partition, str) or not event_partition.isdecimal():
            raise ValueError("Invalid event partition")
        return process_event_partition(event_partition, protocol, started)
    if derivative:
        return derive_batch(identities, protocol, owner=modal.current_function_call_id())
    if (not isinstance(identities, list) or not 1 <= len(identities) <= outbox.MAX_FILES
            or len(set(identities)) != len(identities)):
        raise ValueError("Invalid V3 batch")
    with TemporaryDirectory(prefix="delivery-media-") as workspace:
        return archive_originals(identities, protocol, workspace, started)


def process_event_partition(partition, protocol, started):
    # Wait until the producer window closes, plus a small enqueue grace period.
    close_at = (int(partition) + 1) * v3.EVENT_WINDOW + 1
    delay = close_at - time.time()
    if delay > v3.EVENT_WINDOW + 1:
        raise ValueError("Future event partition")
    if delay > 0:
        time.sleep(delay)
    queue = modal.Queue.from_name(v3.app_name() + "-events", create_if_missing=True)
    hints = v3.deadline(queue.get_many.aio(32, block=False, partition=partition), 5)
    metrics = {"event_hints": len(hints or []), "archived": 0}
    owner = modal.current_function_call_id()
    ended = v3.terminal_cache(modal)

    def candidates():
        for identity in dict.fromkeys(hints or []):
            if time.monotonic() - started >= 360:
                return
            row = prepare_original(identity, owner)
            if row:
                yield row

    for group in v3.groups(candidates()):
        if time.monotonic() - started >= 360:
            break
        with TemporaryDirectory(prefix="delivery-media-") as workspace:
            result = archive_originals([], protocol, workspace, started, prepared=group,
                                       owner=owner, ended=ended)
        metrics["archived"] += result.get("archived", 0)
    return metrics


def prepare_original(identity, owner):
    try:
        return v3.ready(store, source_records, identity, modal, owner=owner)
    except Exception as exc:
        print("DELIVERY_PREPARE_DEFERRED", identity, type(exc).__name__, flush=True)
        return None


def archive_originals(identities, protocol, workspace, started, *, prepared=None, owner=None, ended=None):
    from runtime.delivery.originals import publish_originals
    todo = []
    owner = owner or modal.current_function_call_id()
    ended = ended or v3.terminal_cache(modal)
    candidates = prepared if prepared is not None else (prepare_original(i, owner) for i in identities)
    for row in candidates:
        if not row:
            continue
        intent, request, n = row
        identity = intent["id"]
        try:
            if not v3.reserve_worker(registry, "original", identity, n, owner=owner, ended=ended):
                continue
            # A grouped readiness result can become stale before acquisition.
            # Recheck only the mutable outcome and attempt, not the generation payload.
            if outbox.read(store, outbox.path(identity, "receipt.json")) is not None:
                continue
            if any(outbox.read(store, outbox.path(identity, leaf)) is not None
                   for leaf in ("blocked.json", "terminal.json")):
                continue
            if outbox.attempts(store, identity)[0] != n:
                continue
            if v3.claim(store, intent, n, owner):
                todo.append(row)
        except Exception as exc:
            print("DELIVERY_CLAIM_DEFERRED", identity, type(exc).__name__, flush=True)
    if not todo:
        return {"archived": 0, "submitted_bytes": 0}
    metrics = {}
    try:
        v3.deadline(volume.reload.aio(), 15)
    except Exception as exc:
        for intent, _, n in todo:
            record_original_failure(intent, n, exc)
        raise
    # Both event and scanner paths group originals by bucket and byte budget.
    local_inputs = {}
    buffer = v3.ReceiptBuffer(store)
    try:
        rows = publish_originals(VOLUME_MOUNT, [request for _, request, _ in todo],
                                 metrics=metrics, workspace=workspace, local_inputs=local_inputs,
                                 on_receipt=lambda i, row: buffer.add((*todo[i][:2], row)))
    finally:
        buffer.close()
    derivative_ids = []
    for (intent, _, n), row in zip(todo, rows):
        if "receipt" not in row:
            record_original_failure(intent, n, row)
            continue
        derivative_ids.append(intent["id"])
    # Receipts and recovery markers are durable before inline derivatives start.
    # Leave time for recovery if uploads consumed most of the function deadline.
    metrics["inline_derivatives"] = 0
    for start in range(0, len(derivative_ids), 4):
        if time.monotonic() - started >= 360:
            break
        try:
            result = derive_batch(derivative_ids[start:start + 4], protocol, local_inputs=local_inputs, owner=owner)
            metrics["inline_derivatives"] += result["processed"]
        except Exception as exc:
            print("DERIVATIVE_DEFERRED", type(exc).__name__, flush=True)
    print("DELIVERY_V3", metrics, flush=True)
    return metrics


def retry_queue():
    return modal.Queue.from_name(v3.app_name() + "-retry-hints", create_if_missing=True)


def enqueue_retry(identity, derivative, n, failure):
    if failure["kind"] != "retry" or n + 1 >= outbox.MAX_ATTEMPTS:
        return
    try:
        hint = (identity, derivative, n + 1, failure["retry_at"])
        v3.deadline(retry_queue().put.aio(hint, block=False), 5)
    except Exception as exc:
        print("DELIVERY_RETRY_HINT_DEFERRED", identity, type(exc).__name__, flush=True)


def record_original_failure(intent, n, error):
    failure = outbox.failure(store, intent, n, error)
    enqueue_retry(intent["id"], False, n, failure)


def retry_known_failures():
    queue = retry_queue()
    hints = v3.deadline(queue.get_many.aio(16, block=False), 5)
    end = time.monotonic() + 50
    dispatched = 0
    seen = set()
    for hint in hints or []:
        if not isinstance(hint, (tuple, list)) or len(hint) != 4:
            continue
        identity, derivative, attempt, due = hint
        if (not isinstance(identity, str) or not v3.ID.fullmatch(identity)
                or type(derivative) is not bool or type(attempt) is not int
                or not 1 <= attempt < outbox.MAX_ATTEMPTS
                or type(due) not in (int, float) or not math.isfinite(due)):
            continue
        hint = (identity, derivative, attempt, due)
        if hint in seen:
            continue
        seen.add(hint)
        try:
            if time.monotonic() >= end:
                # The durable marker is authoritative; do not overrun the scanner.
                break
            if time.time() < due:
                v3.deadline(queue.put.aio(hint, block=False), 5)
                continue
            if derivative:
                if outbox.read(store, outbox.path(identity, "derivative.json")) is not None:
                    continue
                current = v3.derivative_due(store, identity)
            else:
                row = v3.ready(store, source_records, identity, modal)
                current = row[2] if row else None
            if current == attempt and dispatch_once(identity, attempt, derivative=derivative):
                dispatch(archive_batch, [identity], derivative=derivative)
                dispatched += 1
        except Exception as exc:
            print("DELIVERY_RETRY_DEFERRED", identity, type(exc).__name__, flush=True)
            # Requeue an ambiguous failure for the next tick; durable state is rechecked.
            if time.monotonic() < end:
                try:
                    v3.deadline(queue.put.aio(hint, block=False), 5)
                except Exception:
                    pass
    return dispatched


def dispatch(fn, identities, **options):
    async def spawn():
        return await fn.spawn.aio(identities, v3.PROTOCOL, **options)
    return v3.deadline(spawn(), 5).object_id


def dispatch_once(identity, n, *, derivative=False):
    # Avoid a scan repeatedly queueing unclaimed calls. Expiry is only a hint;
    # a crash before spawn is recovered by the next time window.
    epoch = int(time.time() // (360 if derivative else 120))
    key = ("dispatch", derivative, identity, n, epoch)
    return v3.deadline(registry.put.aio(key, True, skip_if_exists=True), 5)


def scan_sharded(stage, fn):
    cursor = modal.Dict.from_name(v3.app_name() + "-scan-cursor", create_if_missing=True)
    key = ("next-shard", stage)
    shards = ("legacy", *"0123456789abcdef")
    start = v3.deadline(cursor.get.aio(key, 0), 5)
    # Share a single deadline and dispatch/readiness budget across shards.
    end = time.monotonic() + 65
    remaining = 32 if stage == "original" else 16
    checks = 128 if stage == "original" else 64
    summaries = []
    for offset in range(5):
        if remaining <= 0 or checks <= 0 or time.monotonic() >= end:
            break
        shard = shards[(start + offset) % len(shards)]
        # Give each remaining shard a fair share of the budget.
        slots = 5 - offset
        summary = fn(shard, end=min(end, time.monotonic() + max(1, (end-time.monotonic())/slots)),
                     limit=max(1, remaining//slots), check_limit=max(1, checks//slots))
        summaries.append(summary)
        remaining -= summary.get("dispatched", 0)
        checks -= summary.get("examined", 0)
        v3.deadline(cursor.put.aio(key, (start + offset + 1) % len(shards)), 5)
    return {"shards": summaries, "errors": sum(x.get("errors", 0) for x in summaries),
            "dispatched": sum(x.get("dispatched", 0) for x in summaries)}


@app.function(**scanner, **({"schedule": modal.Cron("*/15 * * * *")} if scheduled else {}))
def scan():
    v3.assert_build()
    with ThreadPoolExecutor(max_workers=2) as pool:
        retries = pool.submit(retry_known_failures)
        derivatives = pool.submit(scan_sharded, "derivative", scan_derivatives)
        result = scan_sharded("original", scan_originals)
        result["derivatives"] = derivatives.result()
        try:
            result["targeted_retries"] = retries.result()
        except Exception as exc:
            print("DELIVERY_RETRY_QUEUE_DEFERRED", type(exc).__name__, flush=True)
    try:
        result["coordination_gc"] = v3.collect_coordination(registry)
    except Exception as exc:
        print("COORDINATION_GC_DEFERRED", type(exc).__name__, flush=True)
    return result


def scan_originals(shard=None, **budget):
    with v3.count_metadata() as operations:
        summary = scan_original_queue(shard, **budget)
    summary["metadata_operations"] = operations
    print("DELIVERY_SCAN", summary, flush=True)
    return summary


def scan_order(stage, shard=None):
    started = time.monotonic()
    timestamps = {}
    options = {} if shard is None else {"shard": shard}
    identities = list(v3.active_ids(store, derived=stage == "derivative", timestamps=timestamps, **options))
    listing_seconds = time.monotonic() - started
    cursor = None
    if identities:
        cursor = modal.Dict.from_name(v3.app_name() + "-scan-cursor", create_if_missing=True)
        try:
            previous = v3.deadline(cursor.get.aio(stage if shard is None else (stage, shard)), 5)
        except Exception as exc:
            print("DELIVERY_CURSOR_READ_DEFERRED", type(exc).__name__, flush=True)
            previous = None
        offset = (bisect_right(identities, previous, key=lambda i: (timestamps[i], i))
                  if previous else 0)
        identities = identities[offset:] + identities[:offset]

    return identities, timestamps, cursor, listing_seconds


def save_cursor(cursor, stage, timestamps, last_visited, shard=None):
    if last_visited:
        try:
            v3.deadline(cursor.put.aio(stage if shard is None else (stage, shard), (timestamps[last_visited], last_visited)), 5)
        except Exception as exc:
            print("DELIVERY_CURSOR_WRITE_DEFERRED", type(exc).__name__, flush=True)


def scan_original_queue(shard=None, *, end=None, limit=32, check_limit=128):
    errors = count = 0
    calls = []
    end = time.monotonic() + 65 if end is None else end
    identities, timestamps, cursor, listing_seconds = scan_order("original", shard)
    readiness_seconds = 0
    last_visited = None
    examined = oldest_ready_age = dispatched = 0

    def candidates():
        nonlocal errors, count, examined, last_visited, oldest_ready_age, readiness_seconds
        for identity in identities:
            if count >= limit or examined >= check_limit or time.monotonic() >= end:
                return
            examined += 1
            last_visited = identity
            try:
                started = time.monotonic()
                try:
                    row = v3.ready(store, source_records, identity, modal)
                finally:
                    readiness_seconds += time.monotonic() - started
                if row and dispatch_once(identity, row[2]):
                    if timestamps[identity]:
                        oldest_ready_age = max(oldest_ready_age, int(time.time() - timestamps[identity]))
                    count += 1
                    yield row
            except Exception as exc:
                errors += 1
                print("DELIVERY_SCAN_DEFERRED", identity, type(exc).__name__, flush=True)

    for group in v3.groups(candidates()):
        try:
            calls.append(dispatch(archive_batch, [intent["id"] for intent, _, _ in group]))
            dispatched += len(group)
        except Exception as exc:
            errors += 1
            print("DELIVERY_DISPATCH_DEFERRED", type(exc).__name__, flush=True)
    # No get() here: Modal's fixed pool advances as soon as a slot is free.
    save_cursor(cursor, "original", timestamps, last_visited, shard)
    return {"ready": count, "calls": calls, "errors": errors, "indexed": len(identities),
            "examined": examined, "dispatched": dispatched, "listing_seconds": listing_seconds,
            "readiness_seconds": readiness_seconds, "oldest_selected_marker_age_s": oldest_ready_age}


@app.function(image=image, cpu=.25, memory=512, timeout=660, max_containers=1,
              scaledown_window=2, **region_options)
def derivative_worker(identities, protocol=v3.PROTOCOL):
    """Compatibility RPC only; actual media work runs in the shared pool."""
    v3.assert_build(protocol)
    return archive_batch.remote(identities, protocol, derivative=True)


def derive_batch(identities, protocol=v3.PROTOCOL, *, local_inputs=None, owner=None):
    v3.assert_build(protocol)
    if not isinstance(identities, list) or not 1 <= len(identities) <= 4 or len(set(identities)) != len(identities):
        raise ValueError("Invalid derivative batch")
    from runtime.archive.preview import backfill_batch
    grouped = {}
    ended = v3.terminal_cache(modal)
    for identity in identities:
        try:
            intent = v3.load(store, identity)
            row = outbox.read(store, outbox.path(identity, "receipt.json"))
            if not row:
                print("DERIVATIVE_MISSING_ORIGINAL_RECEIPT", identity, flush=True)
                continue
            row = v3.committed_receipt(intent, row)
            existing = outbox.read(store, outbox.path(identity, "derivative.json"))
            if existing is not None:
                outbox.validate_derivative(existing, row)
                v3.remove_marker(store, identity, True)
                continue
            n, last = v3.derivative_attempt(store, identity)
            if v3.reconcile_derivative_failure(store, identity, row, n, last):
                continue
            if last and time.time() < last["retry_at"]:
                continue
            if not v3.reserve_worker(registry, "derivative", identity, n, owner=owner, ended=ended):
                continue
            # A prior attempt may have published failure or success during acquisition.
            if v3.derivative_attempt(store, identity)[0] != n:
                continue
            existing = outbox.read(store, outbox.path(identity, "derivative.json"))
            if existing is not None:
                outbox.validate_derivative(existing, row)
                v3.remove_marker(store, identity, True)
                continue
            grouped.setdefault((row["bucket"], row["kind"]), []).append((identity, row, n))
        except Exception as exc:
            print("DERIVATIVE_DEFERRED", identity, type(exc).__name__, flush=True)
    done = 0
    for (bucket, kind), items in grouped.items():
        receipts = [row for _, row, _ in items]
        try:
            if kind == "image":
                results = backfill_batch(bucket, receipts, volume_root=VOLUME_MOUNT, local_inputs=local_inputs)
            else:
                from runtime.archive.video_poster import backfill_video_batch
                results = backfill_video_batch(bucket, receipts, volume_root=VOLUME_MOUNT, local_inputs=local_inputs)
        except Exception as exc:
            print("DERIVATIVE_BATCH_DEFERRED", bucket, kind, type(exc).__name__, flush=True)
            for identity, row, n in items:
                try:
                    last = v3.derivative_failure(store, identity, n, exc)
                    v3.reconcile_derivative_failure(store, identity, row, n + 1, last)
                    enqueue_retry(identity, True, n, last)
                except Exception as failure_exc:
                    print("DERIVATIVE_FAILURE_DEFERRED", identity, type(failure_exc).__name__, flush=True)
            continue
        for identity, row, n in items:
            try:
                result = results.get(row["url"], {"error": "Missing derivative result"})
                if "error" in result:
                    reason = result["error"]
                    if not result.get("invalid_input"):
                        print("DERIVATIVE_ITEM_DEFERRED", identity, reason, flush=True)
                        last = v3.derivative_failure(store, identity, n, {
                            "kind": "retry", "error": reason})
                        v3.reconcile_derivative_failure(store, identity, row, n + 1, last)
                        enqueue_retry(identity, True, n, last)
                        continue
                    result = {"status": "failed", "error": reason, "source_sha256": row["stored_sha256"]}
                if v3.finish_derivative(store, identity, row, result):
                    done += 1
            except Exception as exc:
                print("DERIVATIVE_RECEIPT_DEFERRED", identity, type(exc).__name__, flush=True)
    print("DERIVATIVE_RECEIPTS_CREATED", done, flush=True)
    return {"processed": done}


def scan_derivatives(shard=None, *, end=None, limit=16, check_limit=64):
    v3.assert_build()
    end = time.monotonic() + 65 if end is None else end
    with v3.count_metadata() as operations:
        ordered, timestamps, cursor, listing_seconds = scan_order("derivative", shard)
        selected = []
        calls = []
        errors = examined = dispatched = 0
        last = None
        for identity in ordered:
            if len(selected) >= limit or examined >= check_limit or time.monotonic() >= end:
                break
            last = identity
            examined += 1
            try:
                n = v3.derivative_due(store, identity)
                if n is not None and dispatch_once(identity, n, derivative=True):
                    selected.append(identity)
            except Exception as exc:
                errors += 1
                print("DERIVATIVE_SCAN_DEFERRED", identity, type(exc).__name__, flush=True)
        for i in range(0, len(selected), 4):
            batch = selected[i:i+4]
            try:
                calls.append(dispatch(archive_batch, batch, derivative=True))
                dispatched += len(batch)
            except Exception as exc:
                errors += 1
                print("DERIVATIVE_DISPATCH_DEFERRED", type(exc).__name__, flush=True)
        save_cursor(cursor, "derivative", timestamps, last, shard)
    summary = dict(calls=calls, errors=errors, indexed=len(ordered), examined=examined,
                   dispatched=dispatched, listing_seconds=listing_seconds, metadata_operations=operations)
    print("DERIVATIVE_SCAN", summary, flush=True)
    return summary
