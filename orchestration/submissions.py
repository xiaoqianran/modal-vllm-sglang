"""One durable reservation per output identity; ambiguous acceptance never spawns twice."""
import time
from uuid import uuid4
from shared.tasks.batch import intent_digest


class SubmissionUncertain(RuntimeError):
    pass


class SubmissionLedger:
    def __init__(self, registry):
        self.registry = registry

    @staticmethod
    def key(prefix, item_id):
        return intent_digest([prefix, item_id])

    def lookup(self, prefix, item_id, execution_hash):
        record = self.registry.get(self.key(prefix, item_id))
        if record and record.get("execution_hash") != execution_hash:
            raise ValueError("Submission execution contract changed; refusing output overwrite")
        return record

    def submit(self, prefix, item_id, execution_hash, spawn):
        key = self.key(prefix, item_id)
        reservation = {"execution_hash": execution_hash, "owner": uuid4().hex,
                       "status": "submitting", "created_at": time.time()}
        if not self.registry.put(key, reservation, skip_if_exists=True):
            record = self.lookup(prefix, item_id, execution_hash)
            if record and record.get("call_id"):
                return record["call_id"]
            raise SubmissionUncertain("Submission reserved but acceptance unknown; query persistent result, never respawn")
        # If spawn or receipt persistence fails, the reservation intentionally stays.
        call_id = spawn()
        self.registry[key] = {**reservation, "status": "queued", "call_id": call_id}
        return call_id
