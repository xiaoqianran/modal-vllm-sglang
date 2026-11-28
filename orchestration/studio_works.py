"""Read-only HF directory browser. No local or remote catalog is written."""
from pathlib import PurePosixPath
import json
from urllib.request import urlopen

from runtime.archive.huggingface import public_url
from runtime.archive.works import (MAX_TASK_BYTES, derivative_path, parse_media,
                                   task_path, validate_task, validate_work_id)


def _tree(bucket, prefix, recursive):
    from huggingface_hub import list_bucket_tree
    # The browser consumes public URLs; do not embed tokens in a page or URL.
    return list_bucket_tree(bucket, prefix=prefix, recursive=recursive, token=False)


def list_works(bucket, *, tree=_tree):
    from huggingface_hub.errors import EntryNotFoundError, HfHubHTTPError
    import httpx
    public_url(bucket, "works")
    works = {}
    try:
        for entry in tree(bucket, "works", False):
            path = entry.path.split("/")
            if getattr(entry, "type", None) != "directory" or len(path) != 2 or path[0] != "works":
                continue
            try:
                identity = validate_work_id(path[1])
            except ValueError:
                continue
            works[identity] = {"id": identity, "name": identity.rsplit("--", 1)[0]}
    except EntryNotFoundError:
        return []
    except (HfHubHTTPError, httpx.HTTPError) as exc:
        raise RuntimeError("HF work directory unavailable") from exc
    return sorted(works.values(), key=lambda row: row["id"], reverse=True)


def read_task(bucket, identity):
    with urlopen(public_url(bucket, task_path(identity)), timeout=15) as response:
        payload = response.read(MAX_TASK_BYTES + 1)
    if len(payload) > MAX_TASK_BYTES:
        raise ValueError("Work task definition too large")
    return validate_task(identity, json.loads(payload))


def work_detail(bucket, identity, *, tree=_tree, load_task=read_task):
    identity = validate_work_id(identity)
    prefix = "works/" + identity
    from huggingface_hub.errors import EntryNotFoundError, HfHubHTTPError
    import httpx
    try:
        files = {entry.path: entry for entry in tree(bucket, prefix, True)
                 if getattr(entry, "type", None) == "file"
                 and entry.path.startswith(prefix + "/") and type(entry.size) is int and entry.size > 0}
    except EntryNotFoundError as exc:
        raise FileNotFoundError("Work directory not found") from exc
    except (HfHubHTTPError, httpx.HTTPError) as exc:
        raise RuntimeError("HF work directory unavailable") from exc
    if task_path(identity) not in files:
        raise FileNotFoundError("Work has no published task definition")
    task = validate_task(identity, load_task(bucket, identity))
    jobs = {(job["item"], job["step"]): job for job in task["jobs"]}
    assets = {}
    for path, entry in sorted(files.items()):
        parsed = parse_media(path)
        if not parsed or parsed["work"] != identity:
            continue
        key = (parsed["item"], parsed["step"])
        job = jobs.get(key)
        if job is None:
            continue  # Unrelated files are not part of this work.
        kind = "video" if parsed["ext"] == "mp4" else "image"
        if key in assets:
            raise ValueError("Ambiguous original files for one work step")
        # Historical media must remain browsable even after its model is retired.
        thumb = derivative_path(path)
        original = public_url(bucket, path)
        preview = public_url(bucket, thumb) if thumb in files else (original if kind == "image" else "")
        inputs = job.get("inputs", {})
        assets[key] = {"id": f"{identity}/{parsed['step']}/{parsed['item']}",
                       "project": identity, "name": PurePosixPath(path).name,
                       "title": parsed["item"] + " / " + parsed["step"], "kind": kind,
                       "url": original, "thumb": preview, "bytes": entry.size,
                       "prompt": inputs.get("prompt"), "params": job.get("params", {}),
                       "seed": inputs.get("seed"), "storage": "cloud"}
    return {"id": identity, "name": task["name"], "task": task,
            "assets": list(assets.values()), "expected": len(jobs), "published": len(assets)}
