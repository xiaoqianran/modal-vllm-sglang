"""Explicit image warmup on the engine itself, before GPU snapshot capture."""
import json
import time

from runtime.serving.inference_api import ImageGenerationClient
from runtime.tasks.task_spec import prepare


def warm_image_engine(spec, *, timeout):
    """Run the full representative shape; never publish the warmup image."""
    item = {"id": "snapshot-warmup", "prompt": "A quiet mountain lake at sunrise.",
            "seed": 0, "params": spec["snapshot_warmup"]["params"]}
    payload, _ = prepare(spec, item)
    started = time.perf_counter()
    print(json.dumps({"event": "snapshot_warmup_start", "service": spec["service"],
                      "params": item["params"]}), flush=True)
    data, _ = ImageGenerationClient(
        f"http://127.0.0.1:{spec.get('port', 8000)}",
        output_directory=spec.get("image_output_directory")
    ).generate(payload, timeout=timeout)
    print(json.dumps({"event": "snapshot_warmup_completed", "service": spec["service"],
                      "seconds": time.perf_counter() - started, "bytes": len(data)}), flush=True)

