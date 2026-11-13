"""Resumable MOSS storyboard batch: one GPU session, streaming downloads, stable checkpoints.

Examples:
    uv run --project 001-moss-image python scripts/generate_again_love_v3.py
    uv run --project 001-moss-image python scripts/generate_again_love_v3.py --dry-run
    uv run --project 001-moss-image python scripts/generate_again_love_v3.py --recover-prefix PREFIX

Future drama: --project my-drama --task tasks/my-drama-scene-001.json --task ...
"""
import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = ROOT / "001-moss-image"
sys.path[:0] = [str(ROOT), str(MODEL_DIR)]
from shared.files import atomic_json, download_volume_file, read_json, sha256_file

DEFAULT_TASKS = [ROOT / "tasks" / ("002-again-love-" + scene + ".json")
                 for scene in ("001-hotel-corridor", "002-social-media",
                               "003-apartment-distance", "004-lip-reading",
                               "005-hidden-video")]


def moment():
    return datetime.now(timezone.utc).isoformat()


def is_image(path):
    if not path.is_file() or path.stat().st_size <= 10000:
        return False
    with path.open("rb") as stream:
        header = stream.read(8)
    return header == b"\x89PNG\r\n\x1a\n" or header[:3] == b"\xff\xd8\xff"


def load_plan(project, task_paths):
    """Stable all-shot plan; existing files do not change its identity."""
    shots = []
    scenes = set()
    for task_path in task_paths:
        task = read_json(task_path)
        scene = task["scene"]
        if task["project"] != project or scene in scenes or not scene.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"Duplicate or invalid task project/scene: {task_path}")
        scenes.add(scene)
        for item in task["items"]:
            shot_id = item["id"]
            if not shot_id.isdigit() or len(shot_id) != 3:
                raise ValueError(f"Invalid shot ID {shot_id!r}: {task_path}")
            remote_id = f"{scene}__{shot_id}"
            prompt = item["prompt"]
            if not isinstance(prompt, str) or not prompt.strip() or len(remote_id) > 64:
                raise ValueError(f"Invalid prompt or remote ID: {remote_id}")
            shots.append({"id": remote_id, "prompt": prompt, "scene": scene,
                          "shot_id": shot_id})
    if len(shots) != len({x["id"] for x in shots}):
        raise ValueError("Duplicate shot IDs")
    # Prefix is stable after partial completion; changed prompts get a new prefix.
    content = json.dumps([(s["id"], s["prompt"]) for s in shots], ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()[:24]
    return shots, f"008-moss-storyboard/{project}/resume-{digest}"


def local_target(base, shot, suffix=".png"):
    return base / shot["scene"] / (shot["shot_id"] + suffix)


def select_pending(base, shots):
    pending = []
    for shot in shots:
        png = local_target(base, shot)
        jpeg = local_target(base, shot, ".jpg")
        found = [p for p in (png, jpeg) if p.exists()]
        if found:
            if len(found) != 1 or not is_image(found[0]):
                raise ValueError(f"Corrupt or ambiguous local output: {png}")
            # Validate sidecar when present; older already-accepted outputs are kept unchanged.
            manifest = base / shot["scene"] / ".runner" / "moss-v3" / (shot["shot_id"] + ".json")
            if manifest.exists():
                saved = read_json(manifest)
                if saved.get("sha256") != sha256_file(found[0]) or saved.get("prompt_sha256") != hashlib.sha256(shot["prompt"].encode("utf-8")).hexdigest():
                    raise ValueError(f"Existing image does not match its task/manifest: {found[0]}")
            continue
        pending.append(shot)
    return pending


@contextmanager
def single_writer(base):
    """Nonblocking OS lock. Stale lock files are safe after a process dies."""
    base.mkdir(parents=True, exist_ok=True)
    lock_path = base / ".runner" / "moss-v3.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as stream:
        stream.seek(0)
        stream.write(b"0")
        stream.flush()
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("Another MOSS scene batch is already running") from exc
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("Another MOSS scene batch is already running") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def receive_image(volume, prefix, base, shot, event, *, legacy=False):
    suffix = Path(event["file"]).suffix.lower()
    if suffix not in (".png", ".jpg", ".jpeg"):
        raise ValueError(f"Unsupported output suffix: {event['file']}")
    if Path(event["file"]).name != event["file"]:
        raise ValueError("Unsafe remote filename")
    target = local_target(base, shot, suffix)
    if target.exists():
        if is_image(target) and sha256_file(target) == event["sha256"]:
            return False
        raise FileExistsError(f"Refusing to overwrite existing file: {target}")
    download_volume_file(volume, f"/{prefix}/{event['file']}", target)
    if not is_image(target) or sha256_file(target) != event["sha256"]:
        target.unlink(missing_ok=True)
        raise ValueError(f"Downloaded image checksum/header mismatch: {shot['id']}")
    manifest = base / shot["scene"] / ".runner" / "moss-v3" / (shot["shot_id"] + ".json")
    atomic_json(manifest, {"remote_prefix": prefix, "remote_id": shot["id"],
                           "file": target.name, "sha256": event["sha256"],
                           "prompt_sha256": hashlib.sha256(shot["prompt"].encode("utf-8")).hexdigest(),
                           "source": "recovered-legacy" if legacy else "streamed",
                           "downloaded_utc": moment()})
    print(f"DOWNLOADED {shot['scene']}/{target.name} SHA256={event['sha256']}", flush=True)
    return True


def recover_legacy(volume, prefix, base, pending):
    """Download every verified completed shot in an interrupted earlier remote batch, without GPU."""
    recovered = 0
    for shot in pending:
        metadata_path = f"/{prefix}/{shot['id']}.json"
        try:
            metadata = json.loads(b"".join(volume.read_file(metadata_path)).decode("utf-8"))
        except Exception as exc:
            # Missing artifacts are normal when the remote task has not reached a shot.
            print(f"NOT_YET_REMOTE {shot['id']} ({type(exc).__name__})", flush=True)
            continue
        if metadata.get("status") != "completed" or metadata.get("file") not in (
                shot["id"] + ".png", shot["id"] + ".jpg", shot["id"] + ".jpeg"):
            raise ValueError(f"Invalid remote manifest: {shot['id']}")
        event = {"file": metadata["file"], "sha256": metadata["sha256"]}
        if receive_image(volume, prefix, base, shot, event, legacy=True):
            recovered += 1
    return recovered


def run(project, paths, *, dry_run=False, recover_prefix=None):
    shots, prefix = load_plan(project, paths)
    base = ROOT / "outputs" / project
    with single_writer(base):
        pending = select_pending(base, shots)
        print(f"PLAN project={project} total={len(shots)} local_done={len(shots)-len(pending)} "
              f"pending={len(pending)} stable_prefix={prefix}", flush=True)
        if dry_run or not pending:
            return
        # Only import Modal when an actual remote fetch or GPU call is necessary.
        spec = importlib.util.spec_from_file_location("moss_stream_backend", MODEL_DIR / "batch_storyboard.py")
        backend = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(backend)
        by_id = {shot["id"]: shot for shot in shots}
        state_path = base / ".runner" / "moss-v3-progress.json"
        state = {"project": project, "total": len(shots), "prefix": prefix,
                 "started_utc": moment(), "status": "running", "last_event": None}
        atomic_json(state_path, state)
        if recover_prefix:
            recovered = recover_legacy(backend.volume, recover_prefix.strip("/"), base, pending)
            print(f"RECOVERED_FROM_LEGACY={recovered}", flush=True)
            pending = select_pending(base, shots)
        if not pending:
            state.update(status="completed", local_done=len(shots), updated_utc=moment())
            atomic_json(state_path, state)
            return
        print(f"MODAL_SUBMIT pending={len(pending)} GPU={backend.GPU}; one function, one model startup", flush=True)
        print("WAITING_FOR_GPU_OR_MODEL (no remote log before worker allocation)", flush=True)
        errors = []
        try:
            with backend.app.run():
                for event in backend.generate_batch_stream.remote_gen(
                        [{"id": s["id"], "prompt": s["prompt"]} for s in pending], prefix):
                    status = event["status"]
                    state["last_event"] = event
                    if status in ("ok", "skipped"):
                        shot = by_id[event["id"]]
                        receive_image(backend.volume, prefix, base, shot, event)
                    elif status == "error":
                        errors.append(event)
                        print(f"SHOT_ERROR {event['id']} {event.get('error')}", flush=True)
                    else:
                        print(f"REMOTE_{status.upper()} {json.dumps(event, ensure_ascii=False)}", flush=True)
                    state.update(local_done=len(shots)-len(select_pending(base, shots)),
                                 updated_utc=moment(), remote_errors=errors)
                    atomic_json(state_path, state)
        except BaseException as exc:
            state.update(status="interrupted", error=str(exc), updated_utc=moment())
            atomic_json(state_path, state)
            print(f"INTERRUPTED: {exc}; rerun the same command to continue", flush=True)
            raise
        remaining = select_pending(base, shots)
        state.update(status="completed" if not remaining else "partial",
                     local_done=len(shots)-len(remaining), updated_utc=moment(),
                     missing=[s["id"] for s in remaining])
        atomic_json(state_path, state)
        print(f"BATCH_DONE local={len(shots)-len(remaining)}/{len(shots)} missing={len(remaining)}", flush=True)
        if remaining:
            raise RuntimeError(f"{len(remaining)} shots still missing; rerun to resume")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="002-again-love")
    parser.add_argument("--task", action="append", type=Path, help="JSON scene task; repeatable")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--recover-prefix", help="Recover verified images from an earlier remote prefix")
    args = parser.parse_args()
    run(args.project, args.task or DEFAULT_TASKS,
        dry_run=args.dry_run, recover_prefix=args.recover_prefix)


if __name__ == "__main__":
    main()
