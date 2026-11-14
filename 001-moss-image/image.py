"""MOSS batch inference using the vLLM server already started by the deployed Class."""
import hashlib
import os
from pathlib import Path
import time

from shared.batch import completed_output, intent_digest, safe_id
from shared.files import atomic_json, sha256_file
from shared.inference_api import VLLMImageClient

MODEL = os.getenv("MOSS_MODEL", "OpenMOSS-Team/MOSS-Image-SFT")
OUTPUT_ROOT = Path("/mnt/modal")
REQUEST_DEFAULTS = {
    "size": "1024x1024",
    "n": 1,
    "num_inference_steps": 50,
    "guidance_scale": 3.0,
    "flow_shift": 5.0,
    "response_format": "b64_json",
    "extra_params": {"uncond_skip_layers": [22], "slg_start": 0.01, "slg_stop": 1.0},
}

def stream_batch(shots: list[dict], output_prefix: str, volume, startup_seconds: float):
    """Reuse one running vLLM process; preserve committed per-shot results and hashes."""
    prefix = Path(output_prefix)
    if prefix.is_absolute() or not prefix.parts or any(
            not part or part in (".", "..") for part in prefix.parts):
        raise ValueError("Unsafe MOSS output prefix")
    for part in prefix.parts:
        safe_id(part)
    ids = [safe_id(shot["id"]) for shot in shots]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate remote shot ID")
    if not shots:
        return
    target = OUTPUT_ROOT / prefix
    target.mkdir(parents=True, exist_ok=True)
    yield {"status": "ready", "total": len(shots), "gpu": os.getenv("MOSS_GPU", "RTX-PRO-6000"),
           "startup_seconds": startup_seconds}
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
