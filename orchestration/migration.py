"""Explicit migration of local records; never submits cloud work."""
from pathlib import Path
import shutil

from orchestration import state
from shared.utils.files import read_json, sha256_file


def migrate_scene(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    workspace = output.parent
    if not source.is_relative_to(workspace) or source == workspace:
        raise ValueError("Migration source must stay inside the workspace")
    plan = read_json(source / "plan.json")
    current = read_json(source / "state.json")
    runtime = state.runtime_root(output)
    target = runtime / "tasks" / plan["project"] / plan["scene"]
    media = output / plan["name"]
    if target.exists():
        raise FileExistsError("Task already migrated: " + str(target))
    # Verify all completed local outputs and collisions before any mutation.
    transfers = []
    for job in plan["jobs"]:
        record = current["jobs"][job["key"]]
        artifact = record.get("local_artifact")
        if not artifact:
            continue
        old = state.local_path(source, artifact["path"])
        new = media / (f"{job['item']}_{job['step']}" + old.suffix)
        if not old.is_file() or sha256_file(old) != artifact["sha256"]:
            raise ValueError("Local artifact missing or changed: " + str(old))
        if new.exists() and sha256_file(new) != artifact["sha256"]:
            raise FileExistsError(str(new))
        transfers.append((old, new, artifact))
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("*.png", "*.jpg", "*.jpeg", "*.webp", "*.mp4"))
    media.mkdir(parents=True, exist_ok=True)
    for old, new, artifact in transfers:
        if not new.exists():
            shutil.copy2(old, new)
        artifact["path"] = new.name
    state.save(target / "state.json", current)
    state.save(target / "layout.json", {"media": str(media), "output": str(output), "logs": str(runtime / "logs")})
    task = workspace / "tasks" / (plan["name"] + ".json")
    if task.is_file():
        state.save(target / "task.json", read_json(task))
    state.record_event(target, "migration", {"source": str(source), "local_artifacts": len(transfers), "submitted": False})
    return target
