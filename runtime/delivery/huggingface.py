"""Dispatch HF delivery to a deployed Modal CPU function. Never transfer bytes to PC2."""
from concurrent.futures import Future
from contextlib import ExitStack
from hashlib import sha256
from pathlib import Path
from tempfile import gettempdir
from threading import Condition
from time import monotonic

from orchestration import model_capabilities
from runtime.utils.files import file_lock

from runtime.archive.config import (ASSET_APP_NAME, ARCHIVE_BATCH_BYTES, ARCHIVE_BATCH_FILES,
                                    ARCHIVE_BATCH_WAIT, archive_source_version)


def archive_from_volume(modal_client, job, record, plan):
    """Only metadata crosses the boundary; remote CPU reads the mounted Volume."""
    kind = model_capabilities.CAPABILITIES[job["uses"]]["output"]
    artifact = record["artifacts"][kind]
    # Integrity/path validation takes place on the Modal CPU worker.
    function = modal_client.Function.from_name(ASSET_APP_NAME, "archive_asset")
    return function.remote(artifact, plan["delivery"], plan["project"])


def archive_request(job, record, plan):
    kind = model_capabilities.CAPABILITIES[job["uses"]]["output"]
    return {"artifact": record["artifacts"][kind], "delivery": plan["delivery"],
            "project": plan["project"]}


class ArchiveBatcher:
    """Per-Backend microbatch and singleflight; uses existing delivery threads."""

    def __init__(self, modal_client, *, wait=None, lock_root=None):
        self.modal = modal_client
        self.version = archive_source_version()
        self.wait = wait
        self.windows = {}
        self.targets = {}
        # OS-backed locks release on process death. Fixed stripes bound disk use.
        self.lock_root = Path(lock_root or Path(gettempdir()) / "modal-archive-locks-v1")
        self.condition = Condition()
        self.pending = {}
        self.inflight = {}

    @staticmethod
    def key(request):
        artifact, settings = request["artifact"], request["delivery"]
        # Do not trust a receipt from a different Volume path before reading it.
        # Distinct paths with identical bytes are coalesced after remote validation.
        return (artifact["storage"], artifact["path"], artifact["type"],
                artifact["size"], artifact["sha256"], settings["bucket"],
                settings["compression"], settings["min_saving"], settings["verify"])

    def archive(self, request):
        key = self.key(request)
        bucket = request["delivery"]["bucket"]
        size = request["artifact"]["size"]
        leader = False
        with self.condition:
            future = self.inflight.get(key)
            if future is None:
                future = self.inflight[key] = Future()
                batch = self.pending.get(bucket)
                now = monotonic()
                if (batch is None or now >= batch["deadline"]
                        or len(batch["items"]) >= batch["target"]
                        or batch["bytes"] + size > ARCHIVE_BATCH_BYTES):
                    window = self.wait if self.wait is not None else self.windows.get(bucket, ARCHIVE_BATCH_WAIT)
                    target = ARCHIVE_BATCH_FILES if self.wait is not None else self.targets.get(bucket, ARCHIVE_BATCH_FILES)
                    batch = {"items": [], "bytes": 0, "deadline": now + window, "target": target}
                    self.pending[bucket] = batch
                    leader = True
                batch["items"].append((key, request, future))
                batch["bytes"] += size
                self.condition.notify_all()
            if leader:
                while (self.pending.get(bucket) is batch and len(batch["items"]) < batch["target"]
                       and batch["bytes"] < ARCHIVE_BATCH_BYTES):
                    remaining = batch["deadline"] - monotonic()
                    if remaining <= 0:
                        break
                    self.condition.wait(remaining)
                if self.pending.get(bucket) is batch:
                    del self.pending[bucket]
                if self.wait is None and batch["bytes"] < ARCHIVE_BATCH_BYTES:
                    previous = self.windows.get(bucket, ARCHIVE_BATCH_WAIT)
                    self.windows[bucket] = (max(0.01, previous * 0.75) if len(batch["items"]) == 1
                                            else min(ARCHIVE_BATCH_WAIT, previous * 1.5))
                    target = self.targets.get(bucket, ARCHIVE_BATCH_FILES)
                    count = len(batch["items"])
                    # Always allow at least two files, so bursts can be detected.
                    self.targets[bucket] = (max(2, target - 1) if count == 1 else
                                            min(ARCHIVE_BATCH_FILES, target + 1) if count >= batch["target"] else target)
        if leader:
            self._dispatch(batch["items"])
        return future.result()

    def _dispatch(self, items):
        try:
            function = self.modal.Function.from_name(ASSET_APP_NAME, "archive_batch")
            requests = [request for _, request, _ in items]
            stripes = sorted({sha256((r["delivery"]["bucket"] + ":" +
                                      r["artifact"]["sha256"]).encode()).hexdigest()[:2]
                              for r in requests})
            # Cross-process exclusion on this host only. A second process still
            # independently validates its own source and the remote asset.
            with ExitStack() as locks:
                for stripe in stripes:
                    locks.enter_context(file_lock(self.lock_root / stripe, timeout=3600))
                response = function.remote(requests, self.version)
            if response.get("archive_version") != self.version or len(response.get("results", [])) != len(items):
                raise RuntimeError("Archive batch contract/version mismatch")
            # Validate all envelopes before resolving any Future.
            rows = response["results"]
            for (_, request, _), row in zip(items, rows):
                if "receipt" in row:
                    receipt = row["receipt"]
                    if (receipt.get("status") != "archived" or receipt.get("verification") != "sha256"
                            or receipt.get("source_artifact") != request["artifact"]
                            or receipt.get("bucket") != request["delivery"]["bucket"]):
                        raise RuntimeError("Archive receipt mismatch")
                elif not isinstance(row.get("error"), str):
                    raise RuntimeError("Invalid archive result")
            for (_, _, future), row in zip(items, rows):
                if "receipt" in row:
                    future.set_result(row["receipt"])
                else:
                    future.set_exception(RuntimeError(row["error"]))
        except Exception as exc:
            for _, _, future in items:
                if not future.done():
                    future.set_exception(exc)
        finally:
            with self.condition:
                for key, _, future in items:
                    if self.inflight.get(key) is future:
                        del self.inflight[key]
