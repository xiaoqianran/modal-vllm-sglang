"""Queue independent image jobs first; collect their results concurrently on the host."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
import hashlib
import time
from pathlib import Path

from orchestration.sdk import Backend, model_specs
from runtime.config.storage import volume_key
from runtime.tasks.job_client import check_record, remote_record
from runtime.tasks.task_spec import prepare, execution_signature, result_prefix
from runtime.utils.files import atomic_json, atomic_write, read_json
from runtime.utils.outputs import validate_path_component


def run_batch(backend, spec, items, output, project, *, workers=4):
    """All accepted calls have durable receipts; do not replay ambiguous submissions."""
    output = Path(output)
    validate_path_component(project)
    if spec["kind"] != "image" or not items or len({x["id"].lower() for x in items}) != len(items):
        raise ValueError("Expected independent image items with unique IDs")
    if not 1 <= workers <= 8:
        raise ValueError("workers must be 1..8")
    fingerprint = backend.fingerprint(spec)
    prefix = result_prefix(spec, project, "001", spec["operation"])
    jobs = []
    for item in items:
        _, signature = prepare(spec, item)
        jobs.append({"id": item["id"], "prefix": prefix, "intent_hash": signature,
                     "execution_hash": execution_signature(signature, fingerprint)})
    output.mkdir(parents=True, exist_ok=True)
    report = output / "batch-report.json"
    # Exclusive run directory marker. Existing accepted calls must be collected, not replayed.
    with (output / ".batch-claim").open("x", encoding="utf-8") as stream:
        stream.write(prefix)
    state = {"status": "submitting", "jobs": jobs, "results": [], "errors": []}
    atomic_json(report, state)
    started = time.perf_counter()
    for item, job in zip(items, jobs):
        try:
            if remote_record(backend.volume, prefix, item["id"]) is not None:
                raise RuntimeError("Remote output already exists")
            job["status"] = "submitting"
            atomic_json(report, state)
            job["call_id"] = backend.submit(spec, item, prefix, fingerprint)
            job["status"] = "queued"
            print("QUEUED", item["id"], job["call_id"], flush=True)
        except Exception as exc:
            job["status"] = "uncertain"
            state["errors"].append({"id": item["id"], "error": str(exc)})
        atomic_json(report, state)
    state["submission_seconds"] = time.perf_counter() - started
    state["status"] = "collecting"
    atomic_json(report, state)

    def collect(job):
        result = backend.modal.FunctionCall.from_id(job["call_id"]).get()
        check_record(job, result)
        if result["status"] != "completed":
            raise RuntimeError("Generation did not complete")
        from PIL import Image
        name = result["file"]
        data = b"".join(backend.volume.read_file(volume_key("/" + prefix + "/" + name)))
        if len(data) != result["bytes"] or hashlib.sha256(data).hexdigest() != result["sha256"]:
            raise ValueError("Remote result integrity mismatch")
        target = output / name
        if target.exists():
            raise FileExistsError(target)
        atomic_write(target, data)
        with Image.open(target) as image:
            image.load()
            width, height = image.size
        return {**result, "call_id": job["call_id"], "width": width, "height": height}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(collect, job): job for job in jobs if job.get("call_id")}
        for future in as_completed(futures):
            job = futures[future]
            try:
                result = future.result()
                state["results"].append(result)
                job["status"] = "completed"
                print("VERIFIED", job["id"], result["execution_seconds"], flush=True)
            except Exception as exc:
                job["status"] = "collection_failed"
                state["errors"].append({"id": job["id"], "error": str(exc)})
            atomic_json(report, state)
    state["results"].sort(key=lambda result: result["id"])
    state["status"] = "completed" if len(state["results"]) == len(items) else "incomplete"
    state["wall_seconds"] = time.perf_counter() - started
    atomic_json(report, state)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service")
    parser.add_argument("task", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    backend = Backend()
    spec = model_specs()[args.service]
    from orchestration.deployment_state import model_source_version
    from runtime.tasks.task_identity import intent_digest
    expected = intent_digest([model_source_version(spec), spec, backend.fingerprint(spec)])
    actual = backend.modal.Function.from_name(spec["app_name"], "provision_info").remote()
    if actual != expected:
        raise RuntimeError(f"Deployment version mismatch: expected={expected}, actual={actual}")
    result = run_batch(backend, spec, read_json(args.task)["items"],
                       args.output, args.project)
    if result["status"] != "completed":
        raise SystemExit("Batch incomplete; inspect receipts before retrying")


if __name__ == "__main__":
    main()

