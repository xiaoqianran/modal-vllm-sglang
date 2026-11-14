"""MiniMax H3 Image: one task contract and resumable images on the deployed GPU Class."""
import os
from pathlib import Path

from model_cache import MODEL, MODEL_REVISION, ENCODER_REVISION, SGLANG_REVISION
from shared.batch import completed_output, counts, intent_digest, safe_id
from shared.files import atomic_json, sha256_file
from shared.inference_api import VLLMImageClient
from shared.minimax_params import parameters

OUTPUT_ROOT = Path("/mnt/modal")


def prepare(item):
    """The same validated, signed generation request on the PC and GPU."""
    if not isinstance(item, dict):
        raise ValueError("Image item must be an object")
    shot_id = safe_id(item.get("id"))
    prompt = item.get("prompt")
    seed = item.get("seed")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20000:
        raise ValueError(f"Invalid prompt: {shot_id}")
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise ValueError(f"Invalid seed: {shot_id}")
    payload = parameters(item.get("params"))
    payload.update(prompt=prompt, seed=seed)
    signature = intent_digest({
        "request": payload, "model_revision": MODEL_REVISION,
        "encoder_revision": ENCODER_REVISION, "engine_revision": SGLANG_REVISION,
    })
    return {"id": shot_id, "payload": payload, "intent_hash": signature}


def image_valid(path):
    if not path.is_file() or path.stat().st_size < 64:
        return False
    with path.open("rb") as stream:
        header = stream.read(8)
    return header == b"\x89PNG\r\n\x1a\n" or header[:3] == b"\xff\xd8\xff"


def image_candidates(folder, shot_id):
    return tuple(folder / (shot_id + suffix) for suffix in (".png", ".jpg", ".jpeg"))


def generate_batch(items, output_prefix, *, volume, port=30000):
    """Run on the existing SGLang process and commit each verified image to Volume."""
    if not isinstance(items, list) or not items:
        raise ValueError("MiniMax batch must contain at least one image")
    prefix = Path(output_prefix)
    if prefix.is_absolute() or not prefix.parts:
        raise ValueError("Invalid output prefix")
    for part in prefix.parts:
        safe_id(part)
    prepared = [prepare(item) for item in items]
    if len({item["id"] for item in prepared}) != len(prepared):
        raise ValueError("Duplicate MiniMax image ID")
    folder = OUTPUT_ROOT / prefix
    folder.mkdir(parents=True, exist_ok=True)
    results = []
    client = VLLMImageClient(f"http://127.0.0.1:{port}")
    for item in prepared:
        shot_id = item["id"]
        metadata = folder / f"{shot_id}.json"
        candidates = image_candidates(folder, shot_id)
        existing = next((p for p in candidates
                         if completed_output(p, metadata, item["intent_hash"], validate=image_valid)), None)
        if existing is not None:
            results.append({"id": shot_id, "status": "skipped",
                            "file": existing.name, "sha256": sha256_file(existing),
                            "intent_hash": item["intent_hash"]})
            continue
        if metadata.exists() or any(p.exists() for p in candidates):
            raise FileExistsError(f"Unverified MiniMax image already exists: {shot_id}")
        blob, suffix = client.generate(item["payload"], timeout=1800)
        if suffix not in (".png", ".jpg", ".jpeg") or len(blob) < 64:
            raise ValueError(f"Invalid generated image: {shot_id}")
        destination = folder / f"{shot_id}{suffix}"
        temporary = destination.with_suffix(suffix + ".part")
        with temporary.open("xb") as stream:
            stream.write(blob)
        if destination.exists():
            raise FileExistsError(f"Refusing to overwrite {destination}")
        os.replace(temporary, destination)
        digest = sha256_file(destination)
        atomic_json(metadata, {
            "id": shot_id, "status": "completed", "file": destination.name,
            "intent_hash": item["intent_hash"], "sha256": digest, "bytes": len(blob),
        })
        volume.commit()
        results.append({"id": shot_id, "status": "completed",
                        "file": destination.name, "sha256": digest,
                        "intent_hash": item["intent_hash"]})
    report = {"model": MODEL, "output_prefix": output_prefix,
              "total": len(prepared), "results": results}
    report.update(counts(results, len(prepared)))
    return report
