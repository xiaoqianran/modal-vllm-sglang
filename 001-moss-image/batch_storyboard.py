"""Batch storyboard image inference: one Modal GPU container, sequential requests."""

import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request

import modal
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.modal_setup import model_volume, runtime_image
from model_cache import get_ready_model
from shared.files import download_volume_file, atomic_json, sha256_file
from shared.batch import safe_id, intent_digest, completed_output, counts
from shared.http import request_json
from shared.runtime import stop_process, wait_for_health

MODEL = os.getenv("MOSS_MODEL", "OpenMOSS-Team/MOSS-Image-SFT")
GPU = os.getenv("MOSS_GPU", "RTX-PRO-6000")
app = modal.App("modal-forge-008-moss-storyboard-batch")
volume = model_volume()
image = runtime_image(
    "vllm/vllm-omni:v0.32.0",
    env={"HF_HOME": "/mnt/modal/007-moss-image",
         "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
         "PYTHONPATH": "/opt/moss"},
    files=((Path(__file__).with_name("model_cache.py"), "/opt/moss/model_cache.py"),),
)
REQUEST_DEFAULTS = {
    "size": "1024x1024",
    "n": 1,
    "num_inference_steps": 50,
    "guidance_scale": 3.0,
    "flow_shift": 5.0,
    "response_format": "b64_json",
    "extra_params": {"uncond_skip_layers": [22], "slg_start": 0.01, "slg_stop": 1.0},
}


@app.function(
    image=image,
    gpu=GPU,
    cpu=8,
    memory=32768,
    volumes={"/mnt/modal": volume},
    min_containers=0,
    max_containers=1,
    scaledown_window=2,
    timeout=3600,
)
def generate_batch(shots: list[dict], output_prefix: str):
    """Reuse one model process for every storyboard shot; persist after each image."""
    get_ready_model(MODEL)
    started = time.perf_counter()
    process = subprocess.Popen(
        ["vllm", "serve", MODEL, "--omni", "--enforce-eager", "--port", "8000"]
    )
    target = Path("/mnt/modal") / output_prefix
    target.mkdir(parents=True, exist_ok=True)
    report = {
        "model": MODEL, "gpu": GPU, "output_prefix": output_prefix,
        "sequential": True, "shots": [], "startup_seconds": None,
    }
    try:
        wait_for_health(process, "http://127.0.0.1:8000/health",
                        timeout=900, interval=3)
        report["startup_seconds"] = round(time.perf_counter() - started, 2)
        print(f"READY startup={report['startup_seconds']}s total={len(shots)} gpu={GPU}", flush=True)

        for index, shot in enumerate(shots, 1):
            shot_id = safe_id(shot["id"])
            destination = target / f"{shot_id}.png"
            metadata = target / f"{shot_id}.json"
            payload = dict(REQUEST_DEFAULTS, prompt=shot["prompt"], seed=1000 + index)
            signature = intent_digest({"model": MODEL, "payload": payload})
            def valid_image(path):
                if path.stat().st_size <= 10000:
                    return False
                with path.open("rb") as stream:
                    header = stream.read(12)
                return (header.startswith(b"\x89PNG\r\n\x1a\n")
                        or header.startswith(b"\xff\xd8\xff"))
            existing = next((candidate for candidate in
                             (destination, destination.with_suffix(".jpg"))
                             if completed_output(candidate, metadata, signature,
                                                 validate=valid_image)), None)
            if existing is not None:
                report["shots"].append({"id": shot_id, "status": "skipped",
                                        "bytes": existing.stat().st_size, "file": existing.name})
                print(f"[{index}/{len(shots)}] SKIP {shot_id}", flush=True)
                continue
            t = time.perf_counter()
            try:
                body = request_json("http://127.0.0.1:8000/v1/images/generations",
                                    payload, timeout=600)
                encoded = body.get("data", [{}])[0].get("b64_json")
                if not encoded:
                    raise ValueError("Image response has no b64_json")
                decoded = base64.b64decode(encoded, validate=True)
                if not decoded[:8].hex() == "89504e470d0a1a0a":
                    # Some backends return JPEG rather than PNG regardless of response format.
                    if decoded[:2].hex() == "ffd8":
                        destination = destination.with_suffix(".jpg")
                    else:
                        raise ValueError("Unknown image data format")
                temporary = destination.with_suffix(destination.suffix + ".part")
                temporary.write_bytes(decoded)
                temporary.replace(destination)
                atomic_json(metadata, {
                    "id": shot_id, "status": "completed",
                    "intent_hash": signature, "sha256": sha256_file(destination),
                    "bytes": len(decoded), "file": destination.name,
                })
                volume.commit()
                elapsed = round(time.perf_counter() - t, 2)
                report["shots"].append({"id": shot_id, "status": "ok", "seconds": elapsed, "bytes": len(decoded), "file": destination.name})
                print(f"[{index}/{len(shots)}] OK {shot_id}: {elapsed}s {len(decoded)} bytes", flush=True)
            except Exception as exc:
                report["shots"].append({"id": shot_id, "status": "error", "error": str(exc)})
                print(f"[{index}/{len(shots)}] ERROR {shot_id}: {exc}", flush=True)
        report["total_seconds"] = round(time.perf_counter() - started, 2)
        report["recorded_at_utc"] = datetime.now(timezone.utc).isoformat()
        report.update(counts(report["shots"], len(shots), completed="ok", failed="error"))
        atomic_json(target / "report.json", report)
        volume.commit()
        return report
    finally:
        stop_process(process)


@app.local_entrypoint()
def main(episode: str = "07"):
    root = Path(__file__).parent
    prompt_dir = root / "storyboard" / f"episode{episode}" / "prompts"
    shots = [
        {"id": f"{episode}-{i:02d}", "prompt": (prompt_dir / f"{episode}-{i:02d}.txt").read_text(encoding="utf-8").strip()}
        for i in range(1, 9)
    ]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_prefix = f"008-moss-storyboard/episode{episode}/{stamp}"
    report = generate_batch.remote(shots, output_prefix)
    local = root / "results" / "storyboard" / f"episode{episode}" / stamp
    local.mkdir(parents=True, exist_ok=True)
    atomic_json(local / "report.json", report)
    for shot in report["shots"]:
        if shot["status"] in ("ok", "skipped"):
            download_volume_file(volume, f"/{output_prefix}/{shot['file']}", local / shot["file"])
            download_volume_file(volume, f"/{output_prefix}/{shot['id']}.json",
                                 local / f"{shot['id']}.json")
    print(json.dumps({"local_output": str(local), "report": report}, ensure_ascii=False, indent=2))
