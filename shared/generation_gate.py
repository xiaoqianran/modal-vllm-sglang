"""One in-flight generation per engine, including asynchronous video jobs."""
import threading

from shared.task_lifecycle import TERMINAL


class GenerationGate:
    def __init__(self):
        self.lock = threading.Lock()
        self.occupied = False
        self.job_id = None

    def acquire(self, status):
        """Hold the lock through submission. Reads never acquire this lock.

        Probe a known job on the next admission; no polling thread or timer
        may accidentally release an unknown or still-running generation.
        """
        if not self.lock.acquire(blocking=False):
            return False
        try:
            if self.occupied and self.job_id:
                if status(self.job_id) in TERMINAL:
                    self.occupied = False
                    self.job_id = None
            if self.occupied:
                self.lock.release()
                return False
            self.occupied = True
            return True
        except Exception:
            self.lock.release()
            raise

    def finish(self, *, completed=False, job_id=None):
        # Unknown acceptance keeps the gate closed until the engine is restarted.
        self.occupied = not completed
        self.job_id = job_id
        self.lock.release()
