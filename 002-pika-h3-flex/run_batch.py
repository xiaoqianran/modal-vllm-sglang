"""Modal-run batch video pipeline: T2VA or FL2VA, cached models, one GPU."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

import modal
from model_cache import READY_FILE, get_ready_model

ROOT = Path(__file__).resolve().parent
MODEL = "Pika-Labs/Pika-H3-Flex"
GPU = os.getenv("PIKA_GPU", "RTX-PRO-6000")
APP_NAME = "modal-forge-009-pika-h3-flex-batch"
PORT = 30010
VOLUME_ROOT = "/mnt/modal"
MODEL_CACHE = "/mnt/modal/009-pika-h3-flex"
VALID_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

app = modal.App(APP_NAME)
volume = modal.Volume.from_name("modal-forge", create_if_missing=True)
image = (modal.Image.from_registry("lmsysorg/sglang:dev")
         .entrypoint([])
         .env({"HF_HOME": MODEL_CACHE, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
               "SGLANG_DIFFUSION_CACHE_ROOT": MODEL_CACHE + "/sglang"})
         .env({"PYTHONPATH": "/opt/pika"})
         .add_local_file(str(ROOT / "model_cache.py"), "/opt/pika/model_cache.py")
         )


def json_read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(path)


def safe_id(value):
    if not isinstance(value, str) or not VALID_ID.fullmatch(value):
        raise ValueError(f"Invalid name or id: {value!r}")
    return value


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


def sha256_file(path):
    hash_obj = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hash_obj.update(chunk)
    return hash_obj.hexdigest()


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
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def http_json(path, body=None, timeout=600):
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def http_bytes(path, timeout=600):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}" + path, timeout=timeout) as response:
        return response.read()


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


@app.function(image=image, cpu=4, memory=8192, timeout=900)
def preflight_runtime():
    """Check the Diffusion CLI and Pika registry before allocating a GPU."""
    import importlib.util
    spec = importlib.util.find_spec("sglang.multimodal_gen")
    if spec is None:
        raise RuntimeError("SGLang Diffusion package is missing")
    package = Path(spec.submodule_search_locations[0])
    cmd = [
        "python", "-m", "sglang.multimodal_gen.runtime.entrypoints.cli.main",
        "serve", "--help",
    ]
    check = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    help_text = check.stdout + check.stderr
    candidates = []
    for path in package.rglob("*.py"):
        if "pika" in path.name.lower():
            candidates.append(str(path.relative_to(package)))
    model_strings = []
    for path in package.rglob("*.py"):
        # Look for explicit model registration, not a generic H3 implementation.
        if path.stat().st_size > 500_000:
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if "Pika-H3-Flex" in source or "PikaH3Flex" in source:
            model_strings.append(str(path.relative_to(package)))
    result = {
        "diffusion_module": str(package),
        "cli_exit_code": check.returncode,
        "supports_performance_mode": "--performance-mode" in help_text,
        "supports_layerwise_offload": "--layerwise-offload-components" in help_text,
        "supports_layerwise_resident_layers": "--layerwise-resident-layers" in help_text,
        "pika_files": candidates[:20],
        "pika_registry_references": model_strings[:20],
    }
    print(json.dumps(result, indent=2), flush=True)
    if check.returncode != 0 or any(flag not in help_text for flag in ("--performance-mode", "--layerwise-offload-components", "--layerwise-resident-layers")):
        raise RuntimeError("Diffusion CLI failed preflight")
    if not (candidates or model_strings):
        raise RuntimeError("This image does not register Pika H3 Flex")
    return result


@app.function(
    image=image, gpu=GPU, cpu=12, memory=131072,
    volumes={VOLUME_ROOT: volume},
    min_containers=0, max_containers=1, scaledown_window=2,
    timeout=14400,
)
def generate_batch(workflow: dict, items: list[dict], output_prefix: str):
    """Load the checkpoint once, then generate every item sequentially."""
    # A missing/incomplete CPU-prepared snapshot must not trigger a GPU-side download.
    model_path = get_ready_model()
    model_manifest_hash = hashlib.sha256(READY_FILE.read_bytes()).hexdigest()
    output_root = Path(VOLUME_ROOT) / output_prefix
    output_root.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    report = {
        "model": MODEL, "gpu": GPU, "mode": workflow["mode"],
        "model_manifest_sha256": model_manifest_hash,
        "output_prefix": output_prefix, "startup_seconds": None,
        "results": [], "total": len(items),
    }
    process = subprocess.Popen([
        "python", "-m", "sglang.multimodal_gen.runtime.entrypoints.cli.main",
        "serve", "--model-path", model_path,
        "--performance-mode", "memory",
        "--layerwise-offload-components", "text_encoder,vae",
        "--layerwise-resident-layers", "video_vae=36",
        "--host", "127.0.0.1", "--port", str(PORT),
    ])
    try:
        for _ in range(1200):
            if process.poll() is not None:
                raise RuntimeError(f"SGLang exited while loading: {process.returncode}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=3) as resp:
                    if resp.status == 200:
                        break
            except (urllib.error.URLError, TimeoutError):
                pass
            time.sleep(2)
        else:
            raise TimeoutError("SGLang model loading exceeded 40 minutes")
        report["startup_seconds"] = round(time.perf_counter() - started, 2)
        print(f"MODEL READY in {report['startup_seconds']}s, items={len(items)}", flush=True)

        for index, item in enumerate(items, 1):
            target = output_root / (item["id"] + ".mp4")
            metadata_path = output_root / (item["id"] + ".json")
            # 更换模型组件或卸载策略后，不允许复用旧视频。
            signature = hashlib.sha256(
                (item["intent_hash"] + model_manifest_hash
                 + "performance=memory;offload=text_encoder,vae;video_vae=36").encode()
            ).hexdigest()
            if metadata_path.is_file() and valid_mp4(target):
                try:
                    previous = json_read(metadata_path)
                    if (previous.get("status") == "completed"
                            and previous.get("intent_hash") == signature
                            and previous.get("sha256") == sha256_file(target)):
                        print(f"[{index}/{len(items)}] SKIP {item['id']}", flush=True)
                        report["results"].append({"id": item["id"], "status": "skipped"})
                        continue
                except (OSError, ValueError, KeyError):
                    pass
            entry_started = time.perf_counter()
            try:
                payload = _payload(workflow, item)
                submitted = http_json("/v1/videos", payload, timeout=3600)
                job_id = submitted["id"]
                deadline = time.monotonic() + 3600
                while time.monotonic() < deadline:
                    state = http_json(f"/v1/videos/{job_id}", timeout=120)
                    if state.get("status") == "completed":
                        break
                    if state.get("status") in ("failed", "cancelled"):
                        raise RuntimeError(str(state))
                    time.sleep(2)
                else:
                    raise TimeoutError(f"Video {job_id} did not complete within 3600s")
                clip = http_bytes(f"/v1/videos/{job_id}/content", timeout=900)
                if len(clip) < 32 or clip[4:8] != b"ftyp":
                    raise ValueError(f"Invalid MP4 response: {len(clip)} bytes")
                temp = target.with_suffix(".mp4.part")
                temp.write_bytes(clip)
                temp.replace(target)
                entry = {
                    "id": item["id"], "status": "completed",
                    "intent_hash": signature, "sha256": hashlib.sha256(clip).hexdigest(),
                    "bytes": len(clip), "video_id": job_id,
                    "elapsed_seconds": round(time.perf_counter() - entry_started, 2),
                }
                write_json(metadata_path, entry)
                volume.commit()
                print(f"[{index}/{len(items)}] OK {item['id']} {entry['elapsed_seconds']}s", flush=True)
            except Exception as exc:
                entry = {"id": item["id"], "status": "failed", "error": str(exc)}
                write_json(output_root / (item["id"] + ".error.json"), entry)
                volume.commit()
                print(f"[{index}/{len(items)}] FAILED {item['id']}: {exc}", flush=True)
                report["results"].append(entry)
                break  # Fail fast, never automatically start the next job after failure.
            report["results"].append(entry)
        report["elapsed_seconds"] = round(time.perf_counter() - started, 2)
        report["failed"] = sum(x["status"] == "failed" for x in report["results"])
        report["completed"] = sum(x["status"] == "completed" for x in report["results"])
        report["skipped"] = sum(x["status"] == "skipped" for x in report["results"])
        report["unattempted"] = len(items) - len(report["results"])
        write_json(output_root / "report.json", report)
        volume.commit()
        return report
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
        volume.commit()


def download_file(volume_path, local_path):
    local_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = local_path.with_suffix(local_path.suffix + ".part")
    with temporary.open("wb") as output:
        for part in volume.read_file(volume_path):
            output.write(part)
    temporary.replace(local_path)


@app.local_entrypoint()
def main(workflow: str = "workflow_t2va.json",
         task: str = "tasks/t2va_demo.json",
         limit: int = 0,
         output: str = "",
         run_id: str = "",
         preflight: bool = False):
    """uv run modal run run_batch.py --workflow ... --task ... --limit 1"""
    if preflight:
        preflight_runtime.remote()
        return
    wf_path = resolve_path(workflow)
    task_path = resolve_path(task)
    settings, request = json_read(wf_path), json_read(task_path)
    mode, entries = validate_task(settings, request, limit)
    name = safe_id(request["name"])
    if run_id:
        safe_id(run_id)
    else:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_prefix = f"009-pika-batch/{mode}/{name}/{run_id}"
    for item in entries:
        item["intent_hash"] = intent_hash(settings, item)

    if mode == "fl2va":
        # Upload images once per unique content. Keep large files out of Modal RPC arguments.
        staged = set()
        with volume.batch_upload(force=True) as batch:
            for item in entries:
                for frame in item["frames"]:
                    ext = Path(frame["path"]).suffix.lower()
                    remote = f"/009-pika-batch/inputs/{frame['sha256']}{ext}"
                    if remote not in staged:
                        batch.put_file(frame["path"], remote)
                        staged.add(remote)
                    frame["volume_path"] = VOLUME_ROOT + remote
        print(f"Staged {len(staged)} unique frames in existing Volume", flush=True)

    report = generate_batch.remote(settings, entries, output_prefix)
    local = resolve_path(output) if output else ROOT / "results" / mode / name / run_id
    local.mkdir(parents=True, exist_ok=True)
    write_json(local / "report.json", report)
    for entry in report["results"]:
        if entry["status"] in ("completed", "skipped"):
            shot_id = entry["id"]
            download_file(f"/{output_prefix}/{shot_id}.mp4", local / (shot_id + ".mp4"))
            download_file(f"/{output_prefix}/{shot_id}.json", local / (shot_id + ".json"))
    print(json.dumps({"local_results": str(local), "report": report}, ensure_ascii=False, indent=2), flush=True)
    if report["failed"] or report["unattempted"]:
        raise RuntimeError("Video batch incomplete; inspect report.json")
