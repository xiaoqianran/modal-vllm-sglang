"""Submit a Pika H3 Flex job, poll status, and save its MP4."""
import argparse
import json
import time
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.inference_api import SGLangVideoClient

MODEL = "Pika-Labs/Pika-H3-Flex"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--request", default=str(Path(__file__).with_name("request.example.json")))
    parser.add_argument("--output", default=str(Path(__file__).parent / "results" / "pika_t2va.mp4"))
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
    client = SGLangVideoClient(args.endpoint)
    started = time.perf_counter()
    job_id = client.submit(payload, timeout=120)
    print(f"Job: {job_id}", flush=True)
    client.wait(
        job_id, timeout=args.timeout_seconds, poll=args.poll_seconds,
        status_timeout=120, attempts=1,
        on_status=lambda job: print(f"Status: {job.get('status')}", flush=True),
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    data = client.content(job_id, timeout=300, attempts=1)
    temporary = output.with_suffix(output.suffix + ".part")
    temporary.write_bytes(data)
    temporary.replace(output)
    report = {
        "model": MODEL, "job_id": job_id, "task": payload["task"],
        "seconds_requested": payload["seconds"],
        "output": str(output), "bytes": len(data),
        "elapsed_seconds": round(time.perf_counter() - started, 2),
    }
    output.with_suffix(".json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))



if __name__ == "__main__":
    main()
