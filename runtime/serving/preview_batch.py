"""Cloud-only H3 preview pairs and immutable original recovery."""
import hashlib
import re
from pathlib import Path
from time import perf_counter
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from runtime.config.storage import volume_key
from runtime.archive.huggingface import public_url
from runtime.archive.config import DEFAULT_ASSET_BUCKET
from runtime.utils.files import atomic_json, atomic_write
from runtime.utils.media import image_format
from runtime.utils.outputs import validate_path_component

MAX_ORIGINAL = 64 * 1024 * 1024


def reference(ref):
    path = ref["path"]
    if (not isinstance(path, str) or not re.fullmatch(r"(?:model-results|workflow-inputs)/[A-Za-z0-9_./-]+", path)
            or any(p in {"", ".", ".."} for p in path.split("/"))):
        raise ValueError("Invalid original path")
    if not re.fullmatch(r"[0-9a-f]{64}", ref["sha256"]):
        raise ValueError("Invalid original digest")
    if type(ref["size"]) is not int or not 0 < ref["size"] <= MAX_ORIGINAL:
        raise ValueError("Original must be at most 64 MiB")
    return {k: ref[k] for k in ("path", "sha256", "size")}


def verified_bytes(chunks, ref):
    data = bytearray()
    digest = hashlib.sha256()
    for chunk in chunks:
        if len(data) + len(chunk) > ref["size"]:
            raise ValueError("Original size mismatch")
        digest.update(chunk)
        data.extend(chunk)
    if len(data) != ref["size"] or digest.hexdigest() != ref["sha256"]:
        raise ValueError("Original checksum mismatch")
    image_format(data)
    return bytes(data)


def read_original(volume, root, ref):
    """Mounted data first, then the authoritative committed Volume view."""
    ref = reference(ref)
    if root is not None:
        root = Path(root).resolve()
        path = (root / ref["path"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Original escapes mount")
        if path.is_file():
            try:
                with path.open("rb") as stream:
                    return verified_bytes(iter(lambda: stream.read(1024 * 1024), b""), ref)
            except ValueError:
                # A stale mount can differ from the authoritative committed bytes.
                pass
    return verified_bytes(volume.read_file(volume_key("/" + ref["path"])), ref)


def cache_reference(ref):
    ref = reference(ref)
    return {**ref, "path": "workflow-inputs/preview-originals/" + ref["sha256"]}


def stage_bytes(volume, ref, data):
    """Never overwrite a content address; verify any concurrent winner."""
    import io
    ref = cache_reference(ref)
    verified_bytes((data,), ref)
    key = volume_key("/" + ref["path"])
    try:
        verified_bytes(volume.read_file(key), ref)
    except FileNotFoundError:
        try:
            with volume.batch_upload(force=False) as batch:
                batch.put_file(io.BytesIO(data), key)
        except Exception as exc:
            from modal.exception import InvalidError
            if not (isinstance(exc, FileExistsError) or
                    isinstance(exc, InvalidError) and str(exc) == f'path "{key}" already exists'):
                raise
            verified_bytes(volume.read_file(key), ref)
    return ref


def prepare_original(volume, root, asset, *, opener=urlopen):
    """CPU-side resolution. Only an actual miss permits PC2 fallback."""
    ref = reference(asset)
    cached = cache_reference(ref)
    for candidate in dict.fromkeys((ref["path"], cached["path"])):
        try:
            read_original(volume, root, {**ref, "path": candidate})
            return {"source": "modal", "artifact": {**ref, "path": candidate}}
        except FileNotFoundError:
            continue
    hf = asset.get("hf")
    if hf:
        candidates = [hf]
    else:
        suffix = Path(ref["path"]).suffix.lower().replace(".jpeg", ".jpg")
        suffixes = [suffix] if suffix in {".png", ".jpg", ".webp"} else [".png", ".jpg", ".webp"]
        digest = ref["sha256"]
        candidates = [{"bucket": DEFAULT_ASSET_BUCKET,
                       "path": f"assets/sha256/{digest[:2]}/{digest}{ext}"}
                      for ext in suffixes]
    for hf in candidates:
        # A lossless re-encode has different bytes. Never mistake a preview
        # or re-encoded file for the declared original identity.
        if hf.get("sha256", ref["sha256"]) != ref["sha256"] or hf.get("size", ref["size"]) != ref["size"]:
            raise ValueError("HF asset does not match original byte identity")
        url = public_url(hf["bucket"], hf["path"])
        try:
            with opener(Request(url, headers={"Accept-Encoding": "identity"}), timeout=120) as response:
                if response.status != 200:
                    raise RuntimeError("Expected complete HF response")
                data = verified_bytes(iter(lambda: response.read(1024 * 1024), b""), ref)
        except HTTPError as exc:
            if exc.code != 404:
                raise
        else:
            return {"source": "hf", "artifact": stage_bytes(volume, ref, data)}
    return {"source": "missing", "artifact": ref}


def stage_local(volume, asset, local_path):
    """Host-only last resort. Caller must first receive a cloud miss."""
    ref = reference(asset)
    with Path(local_path).open("rb") as stream:
        data = verified_bytes(iter(lambda: stream.read(1024 * 1024), b""), ref)
    return stage_bytes(volume, ref, data)


class SerializedCommitVolume:
    """One commit lock per container; forward all other Volume methods."""
    def __init__(self, volume):
        from threading import Lock
        self.volume = volume
        self.lock = Lock()

    def commit(self):
        with self.lock:
            return self.volume.commit()

    def __getattr__(self, name):
        return getattr(self.volume, name)


class PreviewWriter:
    """Single bounded writer: validate/write/commit off the inference thread.

    A receipt is returned only after a successful commit. A failed commit is
    ambiguous, so stop admission and never automatically replay inference.
    """
    def __init__(self, volume, root, prefix, container_id, *, batch_files=4,
                 batch_bytes=64 * 1024 * 1024, max_pending=4,
                 max_pending_bytes=192 * 1024 * 1024, flush_interval=2.0,
                 intents=None, on_commit=None):
        from threading import Condition, Thread
        from collections import deque
        if not (1 <= batch_files <= max_pending and MAX_ORIGINAL <= max_pending_bytes
                and 0 < batch_bytes <= max_pending_bytes and flush_interval > 0):
            raise ValueError("Invalid writer bounds")
        self.volume, self.root, self.prefix = volume, root, prefix
        self.container_id = container_id
        self.intents, self.on_commit = intents or {}, on_commit
        self.batch_files, self.batch_bytes = batch_files, batch_bytes
        self.max_pending, self.max_pending_bytes = max_pending, max_pending_bytes
        self.flush_interval = flush_interval
        self.cv, self.queue = Condition(), deque()
        self.pending = self.pending_bytes = 0
        self.closed, self.error = False, None
        self.records = []
        self.futures = set()
        self.metrics = dict(commits=0, commit_seconds=0.0, write_seconds=0.0,
                            enqueue_wait_seconds=0.0, peak_pending=0,
                            peak_pending_bytes=0, flush_wait_seconds=0.0)
        self.thread = Thread(target=self._run, name="preview-writer", daemon=False)
        self.thread.start()

    def check(self):
        with self.cv:
            if self.error is not None:
                raise RuntimeError("Preview writer failed; inspect committed records") from self.error

    def enqueue(self, pair, kind, blob, seconds, *, source=None):
        # Only cheap bounds/header checks run on the inference thread.
        if not isinstance(blob, bytes) or not 0 < len(blob) <= MAX_ORIGINAL:
            raise ValueError("Generated image exceeds preview limit")
        suffix, _ = image_format(blob)
        relative = f"{self.prefix}/{pair['group']}/{pair['case']}/{kind}{suffix}"
        item = (pair, kind, blob, seconds, source, relative)
        self._admit(item, blob)
        return relative

    def enqueue_publication(self, blob, write):
        from concurrent.futures import Future
        future = Future()
        self._admit((write, "__formal__", blob, 0, future, None), blob, future=future)
        return future

    def _admit(self, item, blob, *, future=None):
        if not isinstance(blob, bytes) or not 0 < len(blob) <= MAX_ORIGINAL:
            raise ValueError("Generated image exceeds writer limit")
        started = perf_counter()
        with self.cv:
            self.cv.wait_for(lambda: self.error is not None or self.closed or
                             (self.pending < self.max_pending and
                              self.pending_bytes + len(blob) <= self.max_pending_bytes))
            self.check()
            if self.closed:
                raise RuntimeError("Preview writer is closed")
            if future is not None:
                self.futures.add(future)
            self.queue.append(item)
            self.pending += 1
            self.pending_bytes += len(blob)
            self.metrics["enqueue_wait_seconds"] += perf_counter() - started
            self.metrics["peak_pending"] = max(self.metrics["peak_pending"], self.pending)
            self.metrics["peak_pending_bytes"] = max(self.metrics["peak_pending_bytes"], self.pending_bytes)
            self.cv.notify_all()

    def _write(self, item, known):
        from PIL import Image
        import io
        pair, kind, blob, seconds, source, relative = item
        start = perf_counter()
        with Image.open(io.BytesIO(blob)) as image:
            if image.size != tuple(map(int, pair["size"].split("x"))):
                raise ValueError("Unexpected image dimensions")
            image.verify()
        ref = {"path": relative, "sha256": hashlib.sha256(blob).hexdigest(), "size": len(blob)}
        row = {"group": pair["group"], "case": pair["case"], "task": kind,
               "status": "completed", "artifact": ref, "render_seconds": seconds,
               "container_id": self.container_id}
        intent = self.intents.get((pair["group"], pair["case"], kind))
        if intent:
            loc = intent["locator"]
            row.update(id=kind, file=Path(relative).name, bytes=ref["size"], sha256=ref["sha256"],
                       intent_hash=loc["intent_hash"], execution_hash=loc["execution_hash"],
                       delivery_id=intent["id"])
        if source:
            row["source"] = known[source] if isinstance(source, str) else source
        target = self.root / relative
        if target.exists() or target.with_suffix(".json").exists():
            raise FileExistsError(target)
        atomic_write(target, blob)
        atomic_json(target.with_suffix(".json"), row)
        known[relative] = ref
        elapsed = perf_counter() - start
        row["write_seconds"] = elapsed
        self.metrics["write_seconds"] += elapsed
        return row

    def _run(self):
        known, batch, batch_size, deadline = {}, [], 0, None
        publications = []
        try:
            while True:
                item = None
                with self.cv:
                    # Never wait for a full microbatch when the producer is
                    # blocked by the byte/count bound, or the deadline expires.
                    while not self.queue and not self.closed:
                        remaining = None if deadline is None else deadline - perf_counter()
                        if remaining is not None and remaining <= 0:
                            break
                        self.cv.wait(remaining)
                    if self.queue:
                        next_size = len(self.queue[0][2])
                        if not batch or (len(batch) < self.batch_files and
                                         batch_size + next_size <= self.batch_bytes and
                                         perf_counter() < deadline):
                            item = self.queue.popleft()
                    done = self.closed and not self.queue and item is None
                if item is not None:
                    if not batch:
                        deadline = perf_counter() + self.flush_interval
                    if item[1] == "__formal__":
                        row, finish = item[0](item[2])
                        publications.append((item[4], row, finish))
                    else:
                        row = self._write(item, known)
                    batch.append(row)
                    batch_size += len(item[2])
                    # Drop image bytes once written, but retain admission credit
                    # until commit so uncommitted output remains bounded.
                    item = None
                    if len(batch) < self.batch_files and batch_size < self.batch_bytes:
                        # Drain already admitted work before committing. If the
                        # next item cannot fit, the next iteration flushes.
                        continue
                if batch:
                    start = perf_counter()
                    self.volume.commit()
                    if publications:
                        # Media and collecting evidence must be committed before
                        # publishing completed receipts for formal jobs.
                        for _, _, finish in publications:
                            finish()
                        self.volume.commit()
                    elapsed = perf_counter() - start
                    self.metrics["commits"] += 1 + bool(publications)
                    self.metrics["commit_seconds"] += elapsed
                    for row in batch:
                        row["batch_commit_seconds"] = elapsed
                    with self.cv:
                        self.records.extend(row for row in batch if "artifact" in row)
                        self.pending -= len(batch)
                        self.pending_bytes -= batch_size
                        self.cv.notify_all()
                    for future, row, _ in publications:
                        with self.cv:
                            self.futures.discard(future)
                        future.set_result(row)
                    publications.clear()
                    if self.on_commit is not None:
                        try:
                            self.on_commit(batch)
                        except Exception as exc:
                            print("PREVIEW_DELIVERY_WAKE_DEFERRED", type(exc).__name__, flush=True)
                    batch, batch_size, deadline = [], 0, None
                if done:
                    return
        except BaseException as exc:
            with self.cv:
                self.error = exc
                self.queue.clear()
                futures = list(self.futures)
                self.futures.clear()
                self.cv.notify_all()
            for future in futures:
                if not future.done():
                    future.set_exception(RuntimeError("Publication uncertain; inspect committed evidence"))

    def close(self):
        started = perf_counter()
        with self.cv:
            self.closed = True
            self.cv.notify_all()
        self.thread.join()
        self.metrics["flush_wait_seconds"] = perf_counter() - started
        self.check()
        return self.records


def run_preview_batch(volume, root, project, pairs, render, *, container_id=None, writer_options=None, intents=None, on_commit=None):
    """One GPU invocation; bounded asynchronous publication, no media in RPC."""
    validate_path_component(project)
    root = Path(root)
    if not isinstance(pairs, list) or not 1 <= len(pairs) <= 32:
        raise ValueError("Expected 1..32 image pairs")
    keys = set()
    for pair in pairs:
        group, case = pair["group"], pair["case"]
        validate_path_component(group)
        validate_path_component(case)
        key = (group.lower(), case.lower())
        if key in keys:
            raise ValueError("Duplicate batch pair")
        keys.add(key)
        if not re.fullmatch(r"[1-9][0-9]*x[1-9][0-9]*", pair["size"]):
            raise ValueError("Invalid image size")
        w, h = map(int, pair["size"].split("x"))
        if max(w,h) > 8192 or w*h > 16777216:
            raise ValueError("Image dimensions exceed preview limits")
        if type(pair["area"]) is not int or not 1024 <= pair["area"] <= 16777216:
            raise ValueError("Invalid min generation area")
        if not all(isinstance(pair.get(k), str) and pair[k].strip() for k in ("prompt", "edit")):
            raise ValueError("Missing prompt")
        if pair.get("source"):
            reference(pair["source"])
    prefix = "model-results/minimax/" + project
    folder = root / prefix
    if folder.exists():
        raise FileExistsError("Existing preview batch; inspect committed records before retrying")
    started = perf_counter()
    plan = {"pairs": pairs, "container_id": container_id,
            "delivery_ids": [i["id"] for i in (intents or {}).values()]}
    atomic_json(folder / "plan.json", plan)
    volume.commit()
    writer = PreviewWriter(volume, root, prefix, container_id, intents=intents,
                           on_commit=on_commit, **(writer_options or {}))
    errors = []
    inference_started = perf_counter()
    try:
        for pair in pairs:
            writer.check()
            start = perf_counter()
            if pair.get("source"):
                ref = reference(pair["source"])
                source = read_original(volume, root, ref)
            else:
                source = render(pair["prompt"], None, pair["size"], pair["area"])
                ref = writer.enqueue(pair, "source", source, perf_counter() - start)
            writer.check()
            start = perf_counter()
            edited = render(pair["edit"], source, pair["size"], pair["area"])
            writer.enqueue(pair, "edit", edited, perf_counter() - start, source=ref)
            del source, edited
    except Exception as exc:
        errors.append({"group": pair["group"], "case": pair["case"], "status": "error",
                       "error": type(exc).__name__, "container_id": container_id})
    finally:
        inference_seconds = perf_counter() - inference_started
        try:
            writer.close()
        except RuntimeError as exc:
            # A failed commit is ambiguous: do not publish a new summary via
            # another commit, which could accidentally expose unverified work.
            raise RuntimeError("Preview publication uncertain; inspect Volume and call receipt") from exc
    records = writer.records + errors
    if errors and intents:
        # Explicitly terminate missing outputs after a normal incomplete return.
        # Never overwrite any committed artifact or retry ambiguous inference.
        completed = {r.get("delivery_id") for r in writer.records}
        for intent in intents.values():
            if intent["id"] not in completed:
                loc = intent["locator"]
                target = root / loc["prefix"] / (loc["id"] + ".json")
                if not target.exists():
                    atomic_json(target, {**loc, "status": "failed"})
    report = {"project": project, "container_id": container_id, "records": records,
              "wall_seconds": perf_counter() - started,
              "inference_loop_seconds": inference_seconds,
              "writer": writer.metrics,
              "status": "incomplete" if errors else "completed"}
    atomic_json(folder / "results.json", report)
    volume.commit()
    return report
