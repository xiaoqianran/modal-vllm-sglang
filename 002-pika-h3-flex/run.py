"""One local Pika CLI: send a JSON batch to the deployed GPU Class."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

import modal

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from shared.batch import intent_digest, safe_id
from shared.files import atomic_json, download_volume_file, read_json, sha256_file
from shared.modal_setup import model_volume
from shared.outputs import component, run_dir, step_dir, record_artifact
from video import MODEL, VOLUME_ROOT, resolve_path, validate_task, intent_hash, execution_hash

APP_NAME = "modal-forge-009-pika-h3-flex-fl2va"
volume = model_volume()
json_read = read_json
write_json = atomic_json


def main(workflow: str = "workflow_t2va.json",
         task: str = "tasks/t2va_demo.json",
         limit: int = 0,
         output: str = "",
         run_id: str = ""):
    """Call the deployed Pika Class, then verify and download video results."""
    wf_path = resolve_path(workflow)
    task_path = resolve_path(task)
    settings, request = json_read(wf_path), json_read(task_path)
    mode, entries = validate_task(settings, request, limit)
    name = safe_id(request["name"])
    component(name)
    for item in entries:
        item["intent_hash"] = intent_hash(settings, item)
    if run_id:
        component(run_id)
    else:
        run_id = "v2-" + intent_digest({"model": MODEL, "workflow": settings,
                "name": name, "project": request.get("project"), "scene": request.get("scene"),
                "items": [item["intent_hash"] for item in entries]})[:20]
    output_prefix = f"009-pika-batch/{mode}/{name}/{run_id}"

    local = run_dir(request.get("project", name), scene=request.get("scene", "001-main"),
                    root=resolve_path(output) if output else None)
    pending = []
    current_manifest = None
    for item in entries:
        shot_id = item["id"]
        target = local / f"{shot_id}.mp4"
        if not target.exists():
            pending.append(item)
            continue
        folder = step_dir(local, shot_id, "video")
        artifact_file = folder / "artifacts.json"
        metadata_file = folder / "metadata.json"
        artifact = json_read(artifact_file).get("artifacts", {}).get("video", {}) if artifact_file.exists() else {}
        metadata = json_read(metadata_file) if metadata_file.exists() else {}
        if current_manifest is None:
            digest = hashlib.sha256()
            for chunk in volume.read_file("009-pika-h3-flex/model-ready.json"):
                digest.update(chunk)
            current_manifest = digest.hexdigest()
        if (artifact.get("sha256") != sha256_file(target)
                or metadata.get("intent_hash") != item["intent_hash"]
                or metadata.get("model_manifest_sha256") != current_manifest
                or metadata.get("execution_hash") != execution_hash(item, current_manifest)):
            raise FileExistsError(f"Existing video differs or is unverified: {target}")
    entries = pending
    if not entries:
        print(f"All requested videos already verified: {local}", flush=True)
        return
    if mode == "fl2va":
        # Only upload frames for still-missing clips, not completed reruns.
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
    report = modal.Cls.from_name(APP_NAME, "Pika")().generate_batch.remote(
        settings, entries, output_prefix
    )
    report_path = local / ".runner" / "pika-batch" / "report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(report_path, report)
    for entry in report["results"]:
        if entry["status"] in ("completed", "skipped"):
            shot_id = entry["id"]
            folder = step_dir(local, shot_id, "video")
            video_path = local / f"{shot_id}.mp4"
            if video_path.exists():
                raise FileExistsError(f"Refusing to overwrite {video_path}")
            download_volume_file(volume, f"/{output_prefix}/{shot_id}.mp4", video_path)
            download_volume_file(volume, f"/{output_prefix}/{shot_id}.json", folder / "metadata.json")
            entry["local_path"] = video_path.relative_to(local).as_posix()
            record_artifact(local, video_path, "video", source=f"/{output_prefix}/{shot_id}.mp4")
    report["entrypoint"] = "pika-deployed-batch"
    write_json(report_path, report)
    print(json.dumps({"local_results": str(local), "report": report}, ensure_ascii=False, indent=2), flush=True)
    if report["failed"] or report["unattempted"]:
        raise RuntimeError("Video batch incomplete; inspect report.json")


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--workflow", default="workflow_t2va.json")
    cli.add_argument("--task", default="tasks/t2va_demo.json")
    cli.add_argument("--limit", type=int, default=0)
    cli.add_argument("--output", default="")
    cli.add_argument("--run-id", default="")
    main(**vars(cli.parse_args()))
