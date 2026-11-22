"""Drive scheduler ticks against fake backends; no remote execution."""
from concurrent.futures import ThreadPoolExecutor
import time
from pipeline.s03_run_shot import tick
from orchestration import sdk


def drain(directory, *, backend, timeout=3, poll=0.001):
    downloads = {}
    deadline = time.monotonic() + timeout
    with ThreadPoolExecutor(max_workers=8) as pool:
        while time.monotonic() < deadline:
            result = tick(directory, backend, downloads, pool)
            records = sdk.snapshot(directory)[1]["jobs"].values()
            if (all(record["status"] in {"completed", "failed"} for record in records)
                    and all(entry["reported"] for entry in downloads.values())):
                return result
            time.sleep(poll)
    raise TimeoutError("Fake scheduler did not finish")
