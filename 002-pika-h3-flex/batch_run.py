"""Run resumable, fixed-configuration Pika H3 Flex T2VA/FL2VA jobs."""
import argparse
import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODEL = "Pika-Labs/Pika-H3-Flex"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


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


def _request_hash(mode, workflow, item, images):
    source = {
        "mode": mode,
        "workflow": workflow,
        "id": item["id"],
        "prompt": item["prompt"],
        "images": [hashlib.sha256(data).hexdigest() for data in images],
        "model": MODEL,
    }
    return hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


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
    if not SAFE_ID.fullmatch(str(item.get("id", ""))):
        raise ValueError(f"Unsafe item id: {item.get('id')}")
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
    return payload, _request_hash(mode, workflow, item, images)


def fetch(base, path, payload=None, timeout=180, attempts=3):
    headers = {}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    last = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(base + path, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read(1500).decode("utf-8", errors="replace")
            if exc.code in (400, 401, 403, 404, 413, 422):
                raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
            last = f"HTTP {exc.code}: {detail}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = str(exc)
        if attempt < attempts - 1:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Request failed: {path}: {last}")


def _valid_video(path):
    if not path.is_file() or path.stat().st_size <= 32:
        return False
    with path.open("rb") as handle:
        return handle.read(12)[4:8] == b"ftyp"

def run(base, workflow_path, task_path, out_root, *, poll=1, wait=3600, continue_errors=False):
    workflow, task = read_json(workflow_path), read_json(task_path)
    if not isinstance(task.get("items"), list) or not task["items"]:
        raise ValueError("Task requires a nonempty items list")
    name = str(task.get("name", ""))
    if not SAFE_ID.fullmatch(name):
        raise ValueError(f"Unsafe task name: {name}")
    ids = [str(item.get("id", "")) for item in task["items"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate item IDs")
    mode = workflow.get("mode")
    output_dir = out_root / mode / name
    output_dir.mkdir(parents=True, exist_ok=True)
    total_start = time.perf_counter()
    results = []

    for index, item in enumerate(task["items"], start=1):
        item_id = str(item["id"])
        video_path = output_dir / f"{item_id}.mp4"
        info_path = output_dir / f"{item_id}.json"
        started = time.perf_counter()
        try:
            payload, intent = build_payload(workflow, item, index)
            if info_path.exists() and _valid_video(video_path):
                previous = read_json(info_path)
                if (previous.get("status") == "completed"
                        and previous.get("intent_hash") == intent
                        and previous.get("sha256") == hashlib.sha256(video_path.read_bytes()).hexdigest()):
                    print(f"[{index}/{len(ids)}] {item_id}: already completed, skip", flush=True)
                    results.append({"id": item_id, "status": "skipped", "elapsed_seconds": 0})
                    continue
            print(f"[{index}/{len(ids)}] {item_id}: submitting {mode}", flush=True)
            job = json.loads(fetch(base, "/v1/videos", payload, timeout=3600, attempts=1))
            video_id = job["id"]
            # Video job state lives in the SGLang process. Poll frequently because
            # Modal is set to the minimal scaledown window.
            status = None
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                status = json.loads(fetch(base, f"/v1/videos/{video_id}", attempts=2))
                state = status.get("status")
                if state == "completed":
                    break
                if state in ("failed", "cancelled"):
                    raise RuntimeError(f"Video {state}: {status}")
                time.sleep(poll)
            else:
                raise TimeoutError(f"Job {video_id} exceeded {wait}s")
            blob = fetch(base, f"/v1/videos/{video_id}/content", timeout=600, attempts=2)
            if len(blob) <= 32 or blob[4:8] != b"ftyp":
                raise ValueError("SGLang returned invalid MP4 content")
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
                "output": str(video_path),
            }
            save_json(info_path, record)
            results.append({"id": item_id, "status": "completed", "elapsed_seconds": record["elapsed_seconds"]})
            print(f"[{index}/{len(ids)}] {item_id}: complete in {record['elapsed_seconds']}s", flush=True)
        except Exception as exc:
            record = {"id": item_id, "status": "failed", "error": str(exc)}
            results.append(record)
            print(f"[{index}/{len(ids)}] {item_id}: FAILED: {exc}", flush=True)
            # Preserve previous successful metadata if new intent failed.
            save_json(output_dir / f"{item_id}.error.json", record)
            if not continue_errors:
                break
    report = {
        "name": name, "mode": mode, "model": MODEL,
        "total": len(ids),
        "completed": sum(r["status"] == "completed" for r in results),
        "skipped": sum(r["status"] == "skipped" for r in results),
        "failed": sum(r["status"] == "failed" for r in results),
        "unattempted": len(ids) - len(results),
        "elapsed_seconds": round(time.perf_counter() - total_start, 2),
        "items": results,
    }
    save_json(output_dir / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True, help="Authenticated 009 Modal URL")
    parser.add_argument("--workflow", required=True, help="Fixed workflow JSON")
    parser.add_argument("--task", required=True, help="Prompts/task JSON")
    parser.add_argument("--output", default=str(ROOT / "results"))
    parser.add_argument("--poll", type=float, default=1.0)
    parser.add_argument("--wait", type=int, default=3600)
    parser.add_argument("--continue-errors", action="store_true")
    args = parser.parse_args()
    result = run(
        args.endpoint.rstrip("/"), _path(args.workflow), _path(args.task),
        _path(args.output), poll=args.poll, wait=args.wait,
        continue_errors=args.continue_errors,
    )
    if result["failed"] or result["unattempted"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
