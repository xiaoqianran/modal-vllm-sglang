"""MiniMax H3: run one JSON image task through the deployed H3 Class."""
import argparse
import json
from pathlib import Path
import sys

import modal

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from shared.batch import intent_digest, safe_id
from shared.files import atomic_json, download_volume_file, read_json, sha256_file
from shared.modal_setup import model_volume
from shared.outputs import run_dir, step_dir, record_artifact
from image import image_candidates, image_valid, prepare

APP_NAME = "modal-forge-005-minimax-h3-image"


def task_plan(path):
    data = read_json(path)
    if not isinstance(data, dict) or data.get("version") != 1 or data.get("workflow") != "minimax-t2i":
        raise ValueError("Expected a version-1 minimax-t2i task JSON")
    name = safe_id(data.get("name"))
    scene = safe_id(data.get("scene", "001-main"))
    raw = data.get("items")
    if not isinstance(raw, list) or not raw:
        raise ValueError("MiniMax task requires a nonempty items list")
    options = data.get("params", {})
    if not isinstance(options, dict):
        raise ValueError("Task params must be an object")
    items = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Image item must be an object")
        requested = dict(item)
        requested["params"] = {**options, **item.get("params", {})}
        item_plan = prepare(requested)
        items.append((requested, item_plan))
    if len({p["id"] for _, p in items}) != len(items):
        raise ValueError("Duplicate MiniMax IDs")
    prefix = f"005-minimax-h3-image/{name}/batch-" + intent_digest([
        {"id": p["id"], "intent_hash": p["intent_hash"]} for _, p in items
    ])[:20]
    return name, scene, items, prefix


def completed_local(folder, plan):
    shot_id = plan["id"]
    existing = [p for p in image_candidates(folder, shot_id) if p.exists()]
    if not existing:
        return False
    if len(existing) != 1 or not image_valid(existing[0]):
        raise FileExistsError(f"Existing MiniMax image is ambiguous or invalid: {shot_id}")
    metadata_file = folder / ".runner" / "items" / shot_id / "image" / "metadata.json"
    artifact_file = folder / ".runner" / "items" / shot_id / "image" / "artifacts.json"
    if not metadata_file.is_file() or not artifact_file.is_file():
        raise FileExistsError(f"Existing image lacks verified metadata: {shot_id}")
    record = read_json(metadata_file)
    artifact = read_json(artifact_file).get("artifacts", {}).get("image", {})
    digest = sha256_file(existing[0])
    if (record.get("intent_hash") != plan["intent_hash"]
            or record.get("sha256") != digest
            or record.get("file") != existing[0].name
            or artifact.get("sha256") != digest):
        raise FileExistsError(f"Existing MiniMax image does not match task: {shot_id}")
    return True


def receive(volume, prefix, folder, plan, event):
    """Download and verify a complete remote result, without touching old files."""
    shot_id = plan["id"]
    name = event.get("file")
    if not isinstance(name, str) or name not in (
            shot_id + ".png", shot_id + ".jpg", shot_id + ".jpeg"):
        raise ValueError(f"Unsafe or invalid remote image name: {name!r}")
    if event.get("intent_hash") != plan["intent_hash"]:
        raise ValueError(f"Remote image does not match the requested task: {shot_id}")
    if event.get("status") not in ("completed", "skipped"):
        raise ValueError(f"Remote image is not complete: {shot_id}")
    if any(p.exists() for p in image_candidates(folder, shot_id)):
        raise FileExistsError(f"Refusing to overwrite existing MiniMax image: {shot_id}")
    target = folder / name
    download_volume_file(volume, f"/{prefix}/{name}", target)
    if not image_valid(target) or sha256_file(target) != event.get("sha256"):
        target.unlink(missing_ok=True)
        raise ValueError(f"MiniMax image checksum failed: {shot_id}")
    step = step_dir(folder, shot_id, "image")
    atomic_json(step / "metadata.json", {
        "id": shot_id, "status": "completed", "file": name,
        "intent_hash": plan["intent_hash"], "sha256": event["sha256"],
    })
    record_artifact(folder, target, "image", source=f"/{prefix}/{name}")
    return target


def run(task_path, *, output=None, dry_run=False):
    name, scene, items, prefix = task_plan(task_path)
    folder = run_dir(name, scene=scene, root=output) if not dry_run else (
        (Path(output) if output else ROOT / "outputs") / name / scene
    )
    pending = [(raw, plan) for raw, plan in items if not completed_local(folder, plan)]
    print(f"PLAN name={name} scene={scene} total={len(items)} pending={len(pending)} prefix={prefix}", flush=True)
    if dry_run or not pending:
        return {"pending": len(pending), "total": len(items)}
    volume = model_volume()
    # Check committed Volume output first: an interrupted client need not wake the GPU.
    missing = []
    for raw, plan in pending:
        try:
            metadata = json.loads(b"".join(volume.read_file(
                f"/{prefix}/{plan['id']}.json")).decode("utf-8"))
        except FileNotFoundError:
            missing.append((raw, plan))
            continue
        receive(volume, prefix, folder, plan, metadata)
    if not missing:
        print("All missing images recovered from Volume without GPU inference", flush=True)
        return {"pending": 0, "total": len(items)}
    report = modal.Cls.from_name(APP_NAME, "H3")().generate_batch.remote(
        [raw for raw, _ in missing], prefix
    )
    by_id = {plan["id"]: plan for _, plan in missing}
    for event in report["results"]:
        receive(volume, prefix, folder, by_id[event["id"]], event)
    atomic_json(folder / ".runner" / "minimax-batch" / "report.json", report)
    print(json.dumps({"output": str(folder), "report": report},
                     ensure_ascii=False, indent=2), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True, type=Path, help="Task JSON file")
    parser.add_argument("--output", type=Path, help="Optional outputs root directory")
    parser.add_argument("--dry-run", action="store_true", help="Validate and plan without GPU")
    args = parser.parse_args()
    run(args.task, output=args.output, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
