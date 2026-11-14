"""One Pika video contract: validate T2VA/FL2VA and persist verified outputs."""
import hashlib
from pathlib import Path
import time

from shared.batch import safe_id, intent_digest, counts
from shared.files import atomic_json, read_json, sha256_file
from shared.inference_api import SGLangVideoClient
from model_cache import MODEL

ROOT = Path(__file__).resolve().parent
VOLUME_ROOT = "/mnt/modal"
SG_PORT, PROXY_PORT = 30010, 30011
SGLANG_FLAGS = (
    "--performance-mode", "memory",
    "--layerwise-offload-components", "text_encoder,vae",
    "--layerwise-resident-layers", "video_vae=36",
)
RUNTIME_CONFIG = "|".join(SGLANG_FLAGS)


def resolve_path(value):
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def validate_image(path):
    path = Path(path).resolve(strict=True)
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise ValueError(f"Expected PNG/JPEG/WebP: {path}")
    size = path.stat().st_size
    if not 0 < size <= 20 * 1024 * 1024:
        raise ValueError(f"Image size must be 1-20 MiB: {path}")
    with path.open("rb") as f:
        header = f.read(16)
    if not (header.startswith(b"\x89PNG\r\n\x1a\n")
            or header.startswith(b"\xff\xd8\xff")
            or (header.startswith(b"RIFF") and header[8:12] == b"WEBP")):
        raise ValueError(f"Invalid image signature: {path}")
    return path


def validate_task(workflow, task, limit=0):
    mode = workflow.get("mode")
    if mode not in ("t2va", "fl2va"):
        raise ValueError("mode must be t2va or fl2va")
    if workflow.get("num_inference_steps") != 6:
        raise ValueError("Pika H3 Flex requires exactly 6 steps")
    if not 5 <= workflow.get("seconds", 0) <= 15:
        raise ValueError("Video duration must be 5-15 seconds")
    if any(name in workflow for name in ("flow_shift", "audio_flow_shift")):
        raise ValueError("Pika H3 Flex forbids manual shift overrides")
    if workflow.get("num_outputs_per_prompt", 1) != 1:
        raise ValueError("One output per shot supported")
    safe_id(task.get("name"))
    raw = task.get("items")
    if not isinstance(raw, list) or not raw:
        raise ValueError("Task items must be a nonempty list")
    if limit:
        raw = raw[:limit]
    items = []
    seen = set()
    for index, item in enumerate(raw, 1):
        item_id = safe_id(item.get("id"))
        if item_id in seen:
            raise ValueError(f"Duplicate id: {item_id}")
        seen.add(item_id)
        prompt = item.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f"Empty prompt: {item_id}")
        result = {"id": item_id, "prompt": prompt, "seed": int(workflow.get("seed_start", 0)) + index}
        if mode == "fl2va":
            result["frames"] = []
            for frame_name in ("first_frame", "last_frame"):
                if not item.get(frame_name):
                    raise ValueError(f"{item_id}: {frame_name} required")
                frame = validate_image(resolve_path(item[frame_name]))
                result["frames"].append({"path": str(frame), "sha256": sha256_file(frame)})
        items.append(result)
    return mode, items


def intent_hash(workflow, item):
    obj = {
        "model": MODEL, "workflow": workflow, "id": item["id"],
        "prompt": item["prompt"], "seed": item["seed"],
        "frames": [frame["sha256"] for frame in item.get("frames", [])],
    }
    return intent_digest(obj)


def valid_mp4(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size < 32:
        return False
    with path.open("rb") as stream:
        return stream.read(12)[4:8] == b"ftyp"


def _payload(workflow, item):
    data = {
        "model": MODEL, "task": workflow["mode"], "prompt": item["prompt"],
        "seconds": workflow["seconds"], "target": workflow["target"],
        "num_inference_steps": 6, "num_outputs_per_prompt": 1,
        "seed": item["seed"], "conditions": [],
    }
    if workflow["mode"] == "fl2va":
        data["conditions"] = [
            {"type": "image", "uri": "file://" + frame["volume_path"],
             "role": "keyframe", "frame_index": index}
            for frame, index in zip(item["frames"], (0, -1))
        ]
    return data


def execution_hash(item, manifest_sha256):
    return hashlib.sha256((item["intent_hash"] + manifest_sha256 + RUNTIME_CONFIG).encode()).hexdigest()


def verified_result(output, metadata, item, signature):
    if not valid_mp4(output) or not metadata.is_file():
        return False
    record = read_json(metadata)
    return (
        record.get("status") == "completed"
        and record.get("intent_hash") == item["intent_hash"]
        and record.get("execution_hash") == signature
        and record.get("sha256") == sha256_file(output)
    )


def generate_batch(workflow, items, output_prefix, *, volume, model_manifest_sha256, startup_seconds):
    """Reuse an already-running SGLang engine; no subprocess or duplicate GPU pool."""
    parts = Path(output_prefix).parts
    if not parts or Path(output_prefix).is_absolute():
        raise ValueError("Invalid output prefix")
    for part in parts:
        safe_id(part)
    if workflow.get("mode") not in ("t2va", "fl2va"):
        raise ValueError("Unknown Pika workflow mode")
    target_dir = Path(VOLUME_ROOT) / output_prefix
    target_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "model": MODEL, "mode": workflow["mode"], "output_prefix": output_prefix,
        "model_manifest_sha256": model_manifest_sha256, "startup_seconds": startup_seconds,
        "total": len(items), "results": [],
    }
    client = SGLangVideoClient(f"http://127.0.0.1:{SG_PORT}")
    started = time.perf_counter()
    for index, item in enumerate(items, 1):
        shot_id = safe_id(item["id"])
        output = target_dir / f"{shot_id}.mp4"
        metadata = target_dir / f"{shot_id}.json"
        signature = execution_hash(item, model_manifest_sha256)
        if verified_result(output, metadata, item, signature):
            report["results"].append({"id": shot_id, "status": "skipped"})
            continue
        if output.exists() or metadata.exists():
            raise FileExistsError(f"Unverified video or metadata exists: {shot_id}")
        item_started = time.perf_counter()
        try:
            payload = _payload(workflow, item)
            job_id = client.submit(payload, timeout=3600)
            client.wait(job_id, timeout=3600, poll=2, status_timeout=120, attempts=1)
            clip = client.content(job_id, timeout=900, attempts=1)
            tmp = output.with_suffix(".mp4.part")
            with tmp.open("xb") as stream:
                stream.write(clip)
            if output.exists():
                raise FileExistsError(f"Refusing to overwrite {output}")
            tmp.replace(output)
            record = {
                "id": shot_id, "status": "completed",
                "intent_hash": item["intent_hash"], "execution_hash": signature,
                "model_manifest_sha256": model_manifest_sha256,
                "sha256": hashlib.sha256(clip).hexdigest(), "bytes": len(clip),
                "video_id": job_id,
                "elapsed_seconds": round(time.perf_counter() - item_started, 2),
            }
            atomic_json(metadata, record)
            volume.commit()
            report["results"].append(record)
        except Exception as exc:
            record = {"id": shot_id, "status": "failed", "error": str(exc)}
            atomic_json(target_dir / f"{shot_id}.error.json", record)
            volume.commit()
            report["results"].append(record)
            break
        print(f"[{index}/{len(items)}] {shot_id}: {report['results'][-1]['status']}", flush=True)
    report["elapsed_seconds"] = round(time.perf_counter() - started, 2)
    report.update(counts(report["results"], len(items)))
    atomic_json(target_dir / "report.json", report)
    volume.commit()
    return report
