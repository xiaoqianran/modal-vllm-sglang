"""Run resumable, fixed-configuration Pika H3 Flex T2VA/FL2VA jobs."""
import argparse
import base64
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.files import atomic_json, read_json
from shared.batch import safe_id, intent_digest, completed_output, counts
from shared.inference_api import SGLangVideoClient
from shared.outputs import REPO, run_dir, step_dir, record_artifact, component

ROOT = Path(__file__).resolve().parent
MODEL = "Pika-Labs/Pika-H3-Flex"


save_json = atomic_json


def _path(value):
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _validated_frame(raw):
    path = _path(raw).resolve(strict=True)
    if not path.is_file() or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise ValueError(f"Expected PNG/JPEG/WebP frame: {path}")
    contents = path.read_bytes()
    if not contents or len(contents) > 20 * 1024 * 1024:
        raise ValueError(f"Frame size must be 1-20 MiB: {path}")
    if not (contents.startswith(b"\x89PNG\r\n\x1a\n")
            or contents.startswith(b"\xff\xd8\xff")
            or (contents.startswith(b"RIFF") and contents[8:12] == b"WEBP")):
        raise ValueError(f"Invalid image signature: {path}")
    return contents


def _request_hash(mode, workflow, item, images, seed):
    source = {
        "mode": mode,
        "workflow": workflow,
        "id": item["id"],
        "prompt": item["prompt"],
        "images": [hashlib.sha256(data).hexdigest() for data in images],
        "model": MODEL,
        "seed": seed,
    }
    return intent_digest(source)


def build_payload(workflow, item, index):
    mode = workflow["mode"]
    if mode not in ("t2va", "fl2va"):
        raise ValueError("workflow.mode must be t2va or fl2va")
    if int(workflow["num_inference_steps"]) != 6:
        raise ValueError("Pika H3 Flex requires exactly 6 steps")
    if not (5 <= workflow["seconds"] <= 15):
        raise ValueError("Duration must be 5-15 seconds")
    if any(x in workflow for x in ("flow_shift", "audio_flow_shift")):
        raise ValueError("Pika trained sampler forbids shift overrides")
    if not isinstance(item.get("prompt"), str) or not item["prompt"].strip():
        raise ValueError(f"Missing prompt for {item.get('id')}")
    safe_id(str(item.get("id", "")))
    if int(workflow.get("num_outputs_per_prompt", 1)) != 1:
        raise ValueError("The runner saves exactly one video per item")
    payload = {
        "model": MODEL,
        "task": mode,
        "prompt": item["prompt"],
        "seconds": workflow["seconds"],
        "target": workflow["target"],
        "num_inference_steps": 6,
        "num_outputs_per_prompt": 1,
        "seed": int(workflow.get("seed_start", 0)) + index,
        "conditions": [],
    }
    images = []
    if mode == "fl2va":
        for key in ("first_frame", "last_frame"):
            if not item.get(key):
                raise ValueError(f"FL2VA requires both first_frame and last_frame for {item['id']}")
            images.append(_validated_frame(item[key]))
        payload["first_frame_base64"] = base64.b64encode(images[0]).decode("ascii")
        payload["last_frame_base64"] = base64.b64encode(images[1]).decode("ascii")
    return payload, _request_hash(mode, workflow, item, images, payload["seed"])


def _valid_video(path):
    if not path.is_file() or path.stat().st_size <= 32:
        return False
    with path.open("rb") as handle:
        return handle.read(12)[4:8] == b"ftyp"

def run(base, workflow_path, task_path, out_root=None, *, poll=1, wait=3600, continue_errors=False, run_id=None):
    workflow, task = read_json(workflow_path), read_json(task_path)
    if not isinstance(task.get("items"), list) or not task["items"]:
        raise ValueError("Task requires a nonempty items list")
    name = str(task.get("name", ""))
    safe_id(name)
    component(name)
    ids = [str(item.get("id", "")) for item in task["items"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate item IDs")
    for item_id in ids:
        component(item_id)
    mode = workflow.get("mode")
    # Resolve every payload before creating a run or making any paid call.
    prepared = [build_payload(workflow, item, i) for i, item in enumerate(task["items"], 1)]
    output_dir = run_dir(task.get("project", name), scene=task.get("scene", "001-main"), root=out_root)
    snapshot = {"entrypoint": "pika-http-batch", "workflow": workflow, "task": task,
                "signatures": [signature for _, signature in prepared]}
    snapshot_path = output_dir / ".runner" / "pika-http-batch" / "request.json"
    if snapshot_path.exists() and read_json(snapshot_path) != snapshot:
        raise ValueError("Run inputs changed; create a new run instead of overwriting results")
    save_json(snapshot_path, snapshot)
    print(f"RUN_DIR {output_dir}", flush=True)
    total_start = time.perf_counter()
    client = SGLangVideoClient(base)
    results = []

    for index, item in enumerate(task["items"], start=1):
        item_id = str(item["id"])
        folder = step_dir(output_dir, item_id, "video")
        video_path = output_dir / f"{item_id}.mp4"
        info_path = folder / "metadata.json"
        started = time.perf_counter()
        try:
            payload, intent = prepared[index - 1]
            if completed_output(video_path, info_path, intent, validate=_valid_video):
                print(f"[{index}/{len(ids)}] {item_id}: already completed, skip", flush=True)
                results.append({"id": item_id, "status": "skipped", "elapsed_seconds": 0})
                continue
            print(f"[{index}/{len(ids)}] {item_id}: submitting {mode}", flush=True)
            video_id = client.submit(payload, timeout=3600)
            # Video job state lives in the SGLang process. Poll frequently because
            # Modal is set to the minimal scaledown window.
            client.wait(video_id, timeout=wait, poll=poll, status_timeout=180, attempts=2)
            blob = client.content(video_id, timeout=600, attempts=2)
            if video_path.exists():
                raise FileExistsError(f"Refusing to replace existing video {video_path}")
            temp = video_path.with_suffix(".mp4.part")
            temp.write_bytes(blob)
            temp.replace(video_path)
            record = {
                "status": "completed", "model": MODEL, "task": mode,
                "id": item_id, "video_id": video_id, "prompt": item["prompt"],
                "seed": payload["seed"], "intent_hash": intent,
                "sha256": hashlib.sha256(blob).hexdigest(),
                "bytes": len(blob),
                "elapsed_seconds": round(time.perf_counter() - started, 2),
                "completed_at_utc": datetime.now(timezone.utc).isoformat(),
                "output": video_path.relative_to(output_dir).as_posix(),
            }
            save_json(info_path, record)
            record_artifact(output_dir, video_path, "video")
            results.append({"id": item_id, "status": "completed", "elapsed_seconds": record["elapsed_seconds"]})
            print(f"[{index}/{len(ids)}] {item_id}: complete in {record['elapsed_seconds']}s", flush=True)
        except Exception as exc:
            record = {"id": item_id, "status": "failed", "error": str(exc)}
            results.append(record)
            print(f"[{index}/{len(ids)}] {item_id}: FAILED: {exc}", flush=True)
            # Preserve previous successful metadata if new intent failed.
            save_json(folder / "error.json", record)
            if not continue_errors:
                break
    report = {
        "name": name, "mode": mode, "model": MODEL,
        "entrypoint": "pika-http-batch",
        "local_output": str(output_dir),
        "total": len(ids),
        **counts(results, len(ids)),
        "elapsed_seconds": round(time.perf_counter() - total_start, 2),
        "items": results,
    }
    save_json(output_dir / ".runner" / "pika-http-batch" / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True, help="Authenticated 009 Modal URL")
    parser.add_argument("--workflow", required=True, help="Fixed workflow JSON")
    parser.add_argument("--task", required=True, help="Prompts/task JSON")
    parser.add_argument("--output", default=str(REPO / "outputs"), help="Output root; task/run appended")
    parser.add_argument("--run-id", help="Explicit existing run ID to reuse completed results")
    parser.add_argument("--poll", type=float, default=1.0)
    parser.add_argument("--wait", type=int, default=3600)
    parser.add_argument("--continue-errors", action="store_true")
    args = parser.parse_args()
    result = run(
        args.endpoint.rstrip("/"), _path(args.workflow), _path(args.task),
        _path(args.output), poll=args.poll, wait=args.wait,
        continue_errors=args.continue_errors, run_id=args.run_id,
    )
    if result["failed"] or result["unattempted"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
