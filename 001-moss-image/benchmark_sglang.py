"""Standalone SGLang MOSS-Image benchmark. Does not modify vLLM-Omni."""
import base64
import concurrent.futures
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request

import modal

MODEL = "OpenMOSS-Team/MOSS-Image-SFT"
GPU = "RTX-PRO-6000"
BATCH_SIZE = int(os.environ.get("SGLANG_BATCH_SIZE", "1"))
app = modal.App("modal-forge-008-moss-sglang-benchmark")
volume = modal.Volume.from_name("modal-forge", create_if_missing=True)
image = (
    modal.Image.from_registry("lmsysorg/sglang:latest")
    .entrypoint([])
    .uv_pip_install(
        "sglang[diffusion] @ git+https://github.com/sgl-project/sglang.git@41136ca3501cc25671953831a3fba6a6ef022f1e#subdirectory=python"
    )
    # Core package is pinned at 0.7.1 by this model-support SGLang commit.
    # Remove optional stale 0.7.2 cubin/JIT cache from the newer base image.
    # Let the real FlashInfer version guard run; never disable it.
    .run_commands(
        "python -m pip uninstall -y flashinfer-cubin flashinfer-jit-cache flashinfer-jit-cache-sm80 flashinfer-jit-cache-sm89 flashinfer-jit-cache-sm90a flashinfer-jit-cache-sm100a flashinfer-jit-cache-sm103a flashinfer-jit-cache-sm120f",
        """python -c 'import importlib.metadata as m,pathlib,shutil,sysconfig; names=[d.metadata.get("Name","").lower() for d in m.distributions()]; stale=[n for n in names if n=="flashinfer-cubin" or n.startswith("flashinfer-jit-cache")]; assert not stale,stale; p=pathlib.Path(sysconfig.get_paths()["purelib"])/"flashinfer_jit_cache"; print("PURGE_ORPHAN_NAMESPACE",p,p.exists()); shutil.rmtree(p,ignore_errors=True)'""",
        """python -c 'import flashinfer; print("FLASHINFER_IMPORT_OK",flashinfer.__version__)'""",
    )
    .env({"HF_HOME": "/mnt/modal/007-moss-image"})
)


@app.function(
    image=image,
    gpu=GPU,
    cpu=4,
    memory=8192,
    volumes={"/mnt/modal": volume},
    min_containers=0,
    max_containers=1,
    scaledown_window=2,
    timeout=3600,
)
def run_test(shots: list[dict], batch_size: int, quality: str = "exact"):
    """One checkpoint load, with optional concurrent request waves."""
    from importlib.metadata import version
    if "FLASHINFER_DISABLE_VERSION_CHECK" in os.environ:
        raise RuntimeError("Refusing to run with FlashInfer version validation disabled")
    actual_flashinfer = version("flashinfer-python")
    if actual_flashinfer != "0.7.1":
        raise RuntimeError(f"FlashInfer core version drift: {actual_flashinfer}")
    import flashinfer
    print(f"FlashInfer strict check OK: {flashinfer.__version__}", flush=True)
    if quality not in ("exact", "lossless"):
        raise ValueError("quality must be exact or lossless")
    if batch_size not in (1, 4):
        raise ValueError("batch_size must be 1 or 4")
    output_dir = Path("/mnt/modal/008-sglang-benchmark") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir.mkdir(parents=True, exist_ok=True)
    log = output_dir / "server.log"
    started = time.perf_counter()
    with log.open("w", encoding="utf-8") as stream:
        process = subprocess.Popen([
            "python", "-m", "sglang.multimodal_gen.runtime.entrypoints.cli.main", "serve",
            "--model-path", MODEL,
            "--host", "127.0.0.1", "--port", "30000",
            "--batching-max-size", str(batch_size),
            "--batching-delay-ms", "20",
        ], stdout=stream, stderr=subprocess.STDOUT)
        try:
            while time.perf_counter() - started < 900:
                if process.poll() is not None:
                    raise RuntimeError("SGLang exited during startup: " + log.read_text(encoding="utf-8", errors="replace")[-6000:])
                try:
                    with urllib.request.urlopen("http://127.0.0.1:30000/v1/models", timeout=3) as resp:
                        listed = json.load(resp)
                    if listed.get("data"):
                        break
                except Exception:
                    time.sleep(3)
            else:
                raise TimeoutError("SGLang startup timed out: " + log.read_text(encoding="utf-8", errors="replace")[-6000:])
            startup = round(time.perf_counter() - started, 2)
            results = []
            def infer(entry):
                i, shot = entry
                payload = {
                    "model": MODEL,
                    "prompt": shot["prompt"],
                    "size": "1024x1024",
                    "n": 1,
                    "seed": 1001 + i,
                    "num_inference_steps": 50,
                    "guidance_scale": 3.0,
                    "flow_shift": 5.0,
                    "uncond_skip_layers": [22],
                    "slg_start": 0.01,
                    "slg_stop": 1.0,
                    "quality": quality,
                    "response_format": "b64_json",
                }
                req = urllib.request.Request(
                    "http://127.0.0.1:30000/v1/images/generations",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                t = time.perf_counter()
                with urllib.request.urlopen(req, timeout=600) as resp:
                    response = json.load(resp)
                encoded = response["data"][0]["b64_json"]
                raw = base64.b64decode(encoded, validate=True)
                suffix = ".png" if raw[:8].hex() == "89504e470d0a1a0a" else ".jpg" if raw[:2].hex() == "ffd8" else None
                if not suffix:
                    raise ValueError("Unsupported output image encoding")
                (output_dir / (shot["id"] + suffix)).write_bytes(raw)
                seconds = round(time.perf_counter() - t, 2)
                return {"id": shot["id"], "seconds": seconds, "bytes": len(raw)}

            # Submit up to four requests in one wave to exercise dynamic batching.
            for start_index in range(0, len(shots), batch_size):
                wave = list(enumerate(shots))[start_index:start_index + batch_size]
                with concurrent.futures.ThreadPoolExecutor(max_workers=batch_size) as pool:
                    wave_results = list(pool.map(infer, wave))
                results.extend(wave_results)
                for shot_number, item in enumerate(wave_results, start=start_index + 1):
                    print(f"[{shot_number}/{len(shots)}] {item['id']}: {item['seconds']}s", flush=True)
            report = {
                "model": MODEL, "gpu": GPU, "batching_max_size": batch_size,
                "request_mode": "waves-of-" + str(batch_size), "size": "1024x1024",
                "steps": 50, "quality": quality, "startup_seconds": startup,
                "total_seconds": round(time.perf_counter() - started, 2),
                "results": results, "volume_dir": str(output_dir),
            }
            (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            volume.commit()
            return report
        finally:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
            try:
                volume.commit()
            except Exception as exc:
                print(f"Volume commit on exit failed: {exc}", flush=True)



@app.function(image=image, cpu=2, memory=4096, timeout=120)
def inspect_cli():
    """CPU-only dependency check, no GPU allocation."""
    from importlib.metadata import PackageNotFoundError, distributions, version
    if "FLASHINFER_DISABLE_VERSION_CHECK" in os.environ:
        raise RuntimeError("FlashInfer version check is disabled")
    packages = [(d.metadata.get("Name", ""), d.version) for d in distributions()
                if "flashinfer" in d.metadata.get("Name", "").lower()]
    print("PACKAGES", packages, flush=True)
    assert version("flashinfer-python") == "0.7.1"
    stale = [name for name, _ in packages
             if name.lower() == "flashinfer-cubin" or
             name.lower().startswith("flashinfer-jit-cache")]
    assert not stale, stale
    import flashinfer
    print("FLASHINFER_IMPORT_OK", flashinfer.__version__, flush=True)

@app.local_entrypoint()
def main():
    prompt_dir = Path(__file__).parent / "storyboard" / "episode07" / "prompts"
    shots = [{"id": f"07-{i:02d}", "prompt": (prompt_dir / f"07-{i:02d}.txt").read_text(encoding="utf-8").strip()} for i in range(1, 9)]
    if os.environ.get("SGLANG_INSPECT_ONLY") == "1":
        inspect_cli.remote()
        return
    # Run exactly one batch; do not restart a GPU container just for a preliminary image.
    selected = shots if os.environ.get("SGLANG_RUN_EIGHT") == "1" else shots[:1]
    result = run_test.remote(selected, BATCH_SIZE, os.environ.get("SGLANG_QUALITY", "exact"))
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from shared.outputs import run_dir
    from shared.files import atomic_json
    local = run_dir("moss-sglang", Path(result["volume_dir"]).name, benchmark=True)
    atomic_json(local / "report.json", result)
    print("LOCAL_REPORT", local / "report.json", flush=True)
    print("SGLang batch:", json.dumps(result, ensure_ascii=False, indent=2))
