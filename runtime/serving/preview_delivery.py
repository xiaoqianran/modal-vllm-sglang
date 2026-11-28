"""Bridge preview results to the existing durable V3 delivery protocol."""
from runtime.tasks.task_identity import intent_digest, safe_id
from runtime.delivery.config import normalize_delivery
from runtime.archive.config import DEFAULT_ASSET_BUCKET


def preview_intents(project, pairs):
    """Freeze intent identities before GPU admission, without media bytes."""
    safe_id(project)
    result = {}
    delivery = normalize_delivery({"provider": "huggingface", "bucket": DEFAULT_ASSET_BUCKET,
                                   "compression": "none", "verify": "sha256"})
    for pair in pairs:
        prefix = f"model-results/minimax/{project}/{safe_id(pair['group'])}/{safe_id(pair['case'])}"
        for kind in ("source", "edit"):
            if kind == "source" and pair.get("source"):
                continue
            signature = intent_digest({"project": project, "pair": pair, "kind": kind})
            locator = {"prefix": prefix, "id": kind, "intent_hash": signature,
                       "execution_hash": intent_digest({"preview": 1, "intent": signature})}
            payload = {"schema": 1, "locator": locator, "project": project,
                       "kind": "image", "delivery": delivery}
            result[(pair["group"], pair["case"], kind)] = {"id": intent_digest(payload), **payload}
    return result


class DeliveryWake:
    """Bounded metadata-only dispatcher. V3 scan recovers dropped wake hints."""
    def __init__(self, spawn):
        from queue import Queue
        from threading import Thread, Event
        self.stopped = Event()
        self.queue = Queue(maxsize=64)
        self.spawn = spawn
        self.thread = Thread(target=self._run, daemon=True, name="preview-delivery-wake")
        self.thread.start()

    def submit(self, rows):
        from queue import Full
        ids = [r["delivery_id"] for r in rows if r.get("delivery_id")]
        if ids:
            self.submit_ids(ids)

    def submit_ids(self, ids):
        from queue import Full
        if self.stopped.is_set():
            return
        try:
            self.queue.put_nowait(ids)
        except Full:
            print("PREVIEW_DELIVERY_WAKE_DEFERRED", len(ids), flush=True)

    def _run(self):
        from queue import Empty
        while not self.stopped.is_set():
            try:
                ids = self.queue.get(timeout=.25)
            except Empty:
                continue
            if ids is None:
                return
            try:
                self.spawn(ids)
            except Exception as exc:
                print("PREVIEW_DELIVERY_WAKE_DEFERRED", type(exc).__name__, flush=True)

    def close(self):
        from queue import Full
        try:
            self.queue.put_nowait(None)
        except Full:
            pass
        # No waiting for CPU archive or an unavailable dispatch endpoint.
        # Every intent is already durable before generation starts.
        self.thread.join(timeout=0.25)
        self.stopped.set()


def spawn_delivery(ids):
    import modal
    from runtime.delivery import v3
    async def spawn():
        await modal.Function.from_name(v3.app_name(), "archive_batch").spawn.aio(ids, v3.PROTOCOL)
    v3.deadline(spawn(), 5)
