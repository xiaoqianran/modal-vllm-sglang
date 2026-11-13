"""Batch storyboard image inference: one Modal GPU container, sequential requests."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import modal
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.modal_setup import model_volume, runtime_image
from model_cache import get_ready_model
from shared.files import download_volume_file, atomic_json, sha256_file
from shared.batch import safe_id, intent_digest, completed_output, counts
from shared.inference_api import VLLMImageClient
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
    cpu=4,
    memory=8192,
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
        client = VLLMImageClient("http://127.0.0.1:8000")

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
                image_bytes, suffix = client.generate(payload, timeout=600)
                destination = destination.with_suffix(suffix)
                temporary = destination.with_suffix(destination.suffix + ".part")
                temporary.write_bytes(image_bytes)
                temporary.replace(destination)
                atomic_json(metadata, {
                    "id": shot_id, "status": "completed",
                    "intent_hash": signature, "sha256": sha256_file(destination),
                    "bytes": len(image_bytes), "file": destination.name,
                })
                volume.commit()
                elapsed = round(time.perf_counter() - t, 2)
                report["shots"].append({"id": shot_id, "status": "ok", "seconds": elapsed, "bytes": len(image_bytes), "file": destination.name})
                print(f"[{index}/{len(shots)}] OK {shot_id}: {elapsed}s {len(image_bytes)} bytes", flush=True)
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


@app.function(
    image=image,
    gpu=GPU,
    cpu=4,
    memory=8192,
    volumes={"/mnt/modal": volume},
    min_containers=0,
    max_containers=1,
    scaledown_window=2,
    timeout=14400,
)
def generate_batch_stream(shots: list[dict], output_prefix: str):
    """One GPU/model startup; persist and stream each shot, retrying only missing work.

    output_prefix must be stable across retries. A completed result is reused only
    if prompt, model, parameters, seed and SHA256 match its sidecar manifest.
    """
    ids = [safe_id(shot["id"]) for shot in shots]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate remote shot ID")
    if not shots:
        return
    get_ready_model(MODEL)
    target = Path("/mnt/modal") / output_prefix.strip("/")
    target.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        ["vllm", "serve", MODEL, "--omni", "--enforce-eager", "--port", "8000"]
    )
    started = time.perf_counter()
    try:
        wait_for_health(process, "http://127.0.0.1:8000/health",
                        timeout=900, interval=3)
        yield {"status": "ready", "total": len(shots), "gpu": GPU,
               "startup_seconds": round(time.perf_counter() - started, 2)}
        client = VLLMImageClient("http://127.0.0.1:8000")
        for index, shot in enumerate(shots, 1):
            shot_id = shot["id"]
            seed = 1000 + int(hashlib.sha256(shot_id.encode("utf-8")).hexdigest()[:8], 16) % 1000000
            payload = dict(REQUEST_DEFAULTS, prompt=shot["prompt"], seed=seed)
            signature = intent_digest({"model": MODEL, "payload": payload})
            destination = target / f"{shot_id}.png"
            metadata = target / f"{shot_id}.json"
            candidates = (destination, destination.with_suffix(".jpg"), destination.with_suffix(".jpeg"))
            def valid_image(path):
                if path.stat().st_size <= 10000:
                    return False
                with path.open("rb") as stream:
                    return stream.read(3) in (b"\x89PN", b"\xff\xd8\xff")
            existing = next((path for path in candidates
                             if completed_output(path, metadata, signature, validate=valid_image)), None)
            if existing is not None:
                yield {"id": shot_id, "status": "skipped", "file": existing.name,
                       "sha256": sha256_file(existing), "index": index, "total": len(shots)}
                continue
            if any(path.exists() for path in candidates):
                raise FileExistsError(f"Existing remote image has no matching verified manifest: {shot_id}")
            started_shot = time.perf_counter()
            try:
                image_bytes, suffix = client.generate(payload, timeout=600)
                if suffix not in {".png", ".jpg", ".jpeg"} or len(image_bytes) < 10000:
                    raise ValueError(f"Invalid generated image: {shot_id}")
                destination = destination.with_suffix(suffix)
                temporary = destination.with_suffix(destination.suffix + ".part")
                temporary.write_bytes(image_bytes)
                temporary.replace(destination)
                digest = sha256_file(destination)
                atomic_json(metadata, {"id": shot_id, "status": "completed",
                                       "intent_hash": signature, "sha256": digest,
                                       "bytes": len(image_bytes), "file": destination.name})
                volume.commit()
                event = {"id": shot_id, "status": "ok", "file": destination.name,
                         "sha256": digest, "index": index, "total": len(shots),
                         "seconds": round(time.perf_counter() - started_shot, 2)}
                print(f"[{index}/{len(shots)}] SAVED {shot_id}", flush=True)
                yield event
            except Exception as exc:
                yield {"id": shot_id, "status": "error", "error": str(exc),
                       "index": index, "total": len(shots)}
        yield {"status": "finished", "total": len(shots)}
    finally:
        stop_process(process)


@app.local_entrypoint()
def main(episode: str = "07", task_name: str = "", run_id: str = "", scene: str = "001-main"):
    from shared.outputs import component, run_dir, step_dir, record_artifact
    component(episode)
    task_name = component(task_name or f"episode{episode}")
    stamp = component(run_id) if run_id else None
    root = Path(__file__).parent
    prompt_dir = root / "storyboard" / f"episode{episode}" / "prompts"
    shots = [
        {"id": f"{episode}-{i:02d}", "prompt": (prompt_dir / f"{episode}-{i:02d}.txt").read_text(encoding="utf-8").strip()}
        for i in range(1, 9)
    ]
    all_shots = shots
    local = run_dir(task_name, scene=scene)
    pending = []
    for shot in shots:
        existing = local / (shot["id"] + ".png")
        if not existing.exists():
            pending.append(shot)
            continue
        manifest = step_dir(local, shot["id"], "image") / "artifacts.json"
        saved = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
        image = saved.get("artifacts", {}).get("image", {})
        if image.get("sha256") != sha256_file(existing):
            raise FileExistsError(f"Existing image is not verified; refusing GPU regeneration: {existing}")
    shots = pending
    if not shots:
        print(f"All shots already verified: {local}", flush=True)
        return
    if stamp is None:
        stamp = "resume-" + intent_digest({
            "model": MODEL, "params": REQUEST_DEFAULTS, "shots": all_shots,
        })[:24]
    output_prefix = f"008-moss-storyboard/episode{episode}/{stamp}"
    meta = local / ".runner" / "moss-batch"
    meta.mkdir(parents=True, exist_ok=True)
    report = {"output_prefix": output_prefix, "shots": [], "entrypoint": "moss-modal-batch"}
    atomic_json(meta / "report.json", report)
    for event in generate_batch_stream.remote_gen(shots, output_prefix):
        if event["status"] in ("ok", "skipped"):
            folder = step_dir(local, event["id"], "image")
            image_path = local / (event["id"] + Path(event["file"]).suffix)
            if image_path.exists():
                if sha256_file(image_path) != event["sha256"]:
                    raise FileExistsError(f"Existing image differs from remote: {image_path}")
            else:
                download_volume_file(volume, f"/{output_prefix}/{event['file']}", image_path)
            if sha256_file(image_path) != event["sha256"]:
                raise ValueError(f"Image checksum mismatch: {image_path}")
            download_volume_file(volume, f"/{output_prefix}/{event['id']}.json",
                                 folder / "metadata.json")
            record_artifact(local, image_path, "image", source=f"/{output_prefix}/{event['file']}")
            event["local_path"] = image_path.relative_to(local).as_posix()
        if event["status"] in ("ok", "skipped", "error"):
            report["shots"].append(event)
        atomic_json(meta / "report.json", report)
        print(f"STORYBOARD_PROGRESS {event.get('status')} "
              f"{event.get('id', '')} {len(report['shots'])}/{len(shots)}", flush=True)
    if any(x["status"] == "error" for x in report["shots"]):
        raise RuntimeError("Some MOSS storyboard shots failed; rerun to resume")
    atomic_json(meta / "report.json", report)
    print(json.dumps({"local_output": str(local), "report": report}, ensure_ascii=False, indent=2))
