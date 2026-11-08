"""Bounded MOSS-Image GPU benchmark using loopback requests inside Modal."""
import concurrent.futures
from datetime import datetime, timezone
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

import modal
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.modal_setup import model_volume, runtime_image
from model_cache import get_ready_model
from shared.runtime import stop_process, wait_for_health

GPU = os.getenv("MOSS_GPU", "RTX-PRO-6000")
MODEL = os.getenv("MOSS_MODEL", "OpenMOSS-Team/MOSS-Image-SFT")
app = modal.App("modal-forge-008-moss-benchmark")
volume = model_volume()
image = runtime_image(
    "vllm/vllm-omni:v0.32.0",
    env={"HF_HOME": "/root/.cache/huggingface",
         "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
         "PYTHONPATH": "/opt/moss"},
    files=((Path(__file__).with_name("model_cache.py"), "/opt/moss/model_cache.py"),),
)

CACHE = "/root/.cache/huggingface"
REQUEST = {
    "prompt": "A red fox standing in a snowy forest, cinematic lighting",
    "size": "1024x1024",
    "n": 1,
    "num_inference_steps": 50,
    "guidance_scale": 3.0,
    "flow_shift": 5.0,
    "seed": 0,
    "response_format": "b64_json",
    "extra_params": {
        "uncond_skip_layers": [22],
        "slg_start": 0.01,
        "slg_stop": 1.0,
    },
}


@app.function(
    image=image,
    gpu=GPU,
    cpu=8,
    memory=32768,
    volumes={
        CACHE: volume.with_mount_options(sub_path="/007-moss-image"),
    },
    timeout=3600,
)
def run_benchmark():
    """Test warm latency and two parallel requests without exposing a public API."""
    get_ready_model(MODEL)
    started = time.perf_counter()
    process = subprocess.Popen(
        ["vllm", "serve", MODEL, "--omni", "--enforce-eager", "--port", "8000"],
    )
    report = {"engine": "vllm-omni", "model": MODEL, "gpu_requested": GPU}
    try:
        wait_for_health(process, "http://127.0.0.1:8000/health",
                        timeout=1800, interval=5)
        report["startup_seconds"] = round(time.perf_counter() - started, 2)

        def infer(seed):
            payload = dict(REQUEST, seed=seed)
            request = urllib.request.Request(
                "http://127.0.0.1:8000/v1/images/generations",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            t = time.perf_counter()
            with urllib.request.urlopen(request, timeout=1200) as response:
                body = json.load(response)
            if not body.get("data") or not body["data"][0].get("b64_json"):
                raise ValueError("Image response missing b64_json")
            return round(time.perf_counter() - t, 2)

        report["single_seconds"] = infer(0)
        # Concurrent requests test engine concurrency, not multi-container scaling.
        t = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(infer, seed) for seed in (1, 2)]
            report["parallel_each_seconds"] = [f.result() for f in futures]
        report["parallel_total_seconds"] = round(time.perf_counter() - t, 2)
        report["parallel_images_per_minute"] = round(
            120 / report["parallel_total_seconds"], 3
        )
        gpu = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        report["gpu_after_run"] = gpu.stdout.strip()
        return report
    finally:
        stop_process(process)
        try:
            volume.commit()
        except Exception as exc:
            print(f"Volume commit failed: {exc}", flush=True)


@app.local_entrypoint()
def main():
    report = run_benchmark.remote()
    report["recorded_at_utc"] = datetime.now(timezone.utc).isoformat()
    destination = Path(__file__).parent / "results"
    destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = destination / f"vllm_{report['gpu_requested'].lower().replace('-', '_')}_{stamp}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Benchmark report: {output}")
    print(json.dumps(report, ensure_ascii=False, indent=2))
