"""Submit a Pika H3 Flex job, poll status, and save its MP4."""
import argparse
import json
import time
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.inference_api import SGLangVideoClient
from shared.outputs import run_dir, step_dir, record_artifact, component
from shared.files import atomic_json

MODEL = "Pika-Labs/Pika-H3-Flex"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--request", default=str(Path(__file__).with_name("request.example.json")))
    parser.add_argument("--output", help="Optional explicit MP4 path; must not already exist")
    parser.add_argument("--task-name", default="pika-single")
    parser.add_argument("--scene", default="001-main")
    parser.add_argument("--poll-seconds", type=int, default=5)
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    args = parser.parse_args()
    payload = json.loads(Path(args.request).read_text(encoding="utf-8-sig"))
    payload["model"] = MODEL
    if payload.get("num_inference_steps") != 6:
        raise ValueError("Pika H3 Flex requires exactly six steps")
    if "flow_shift" in payload or "audio_flow_shift" in payload:
        raise ValueError("Pika Flex uses a fixed schedule; remove shift overrides")
    if not 5 <= payload.get("seconds", 0) <= 15:
        raise ValueError("Pika Flex duration must be between 5 and 15 seconds")
    component(args.task_name)
    if args.output:
        output = Path(args.output).resolve()
        if output.exists() or output.with_suffix(".json").exists():
            raise ValueError("Output already exists; choose a new destination")
        local = output.parent
    else:
        local = run_dir(args.task_name, scene=args.scene)
        output = local / "001.mp4"
        if output.exists():
            raise FileExistsError(f"Refusing to overwrite {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    metadata = step_dir(local, "001", "video") if not args.output else output.parent
    info_path = output.with_suffix(".json") if args.output else metadata / "metadata.json"
    atomic_json(metadata / "request.json", payload)
    client = SGLangVideoClient(args.endpoint)
    started = time.perf_counter()
    job_id = client.submit(payload, timeout=120)
    atomic_json(info_path, {"status": "submitted", "job_id": job_id})
    print(f"Job: {job_id}", flush=True)
    client.wait(
        job_id, timeout=args.timeout_seconds, poll=args.poll_seconds,
        status_timeout=120, attempts=1,
        on_status=lambda job: print(f"Status: {job.get('status')}", flush=True),
    )
    data = client.content(job_id, timeout=300, attempts=1)
    temporary = output.with_suffix(output.suffix + ".part")
    temporary.write_bytes(data)
    temporary.replace(output)
    report = {
        "model": MODEL, "job_id": job_id, "task": payload["task"],
        "seconds_requested": payload["seconds"],
        "output": output.relative_to(local).as_posix(), "bytes": len(data),
        "entrypoint": "pika-single", "status": "completed",
        "elapsed_seconds": round(time.perf_counter() - started, 2),
    }
    atomic_json(info_path, report)
    if not args.output:
        atomic_json(metadata / "report.json", report)
        record_artifact(local, output, "video")
    print(f"OUTPUT {output}", flush=True)
    print(json.dumps(report, ensure_ascii=False, indent=2))



if __name__ == "__main__":
    main()
