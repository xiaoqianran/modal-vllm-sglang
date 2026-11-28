"""One immutable task definition and deterministic media paths in HF Bucket.

This is a storage convention, not a catalog or a second execution ledger.
"""
import hashlib
import json
import re
from pathlib import PurePosixPath
from urllib.error import HTTPError

from runtime.archive.huggingface import public_url
from runtime.tasks.task_identity import intent_digest

_COMPONENT = r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
_WORK = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}--[0-9a-f]{64}")
_MEDIA = re.compile(rf"works/(?P<work>{_WORK.pattern})/(?P<item>{_COMPONENT})/(?P<step>{_COMPONENT})\.(?P<ext>png|jpg|webp|mp4)")
MAX_TASK_BYTES = 1024 * 1024


def work_id(plan):
    # An immutable execution definition, not a mutable timestamp or a per-shot hash.
    frozen = {k: v for k, v in plan.items() if k not in {"hash", "work"}}
    return f"{plan['name']}--{intent_digest(frozen)}"


def validate_work_id(value):
    if not isinstance(value, str) or not _WORK.fullmatch(value):
        raise ValueError("Invalid work ID")
    return value


def task_definition(plan):
    # Reuse the compiled task definition. Never publish local import paths,
    # Volume locators, credentials, runtime receipts or service configuration.
    jobs = []
    for job in plan["jobs"]:
        inputs = job["inputs"]
        jobs.append({k: job[k] for k in ("key", "item", "step", "uses", "params")} |
                    {"inputs": {k: v for k, v in inputs.items()
                                if isinstance(v, (str, int, float, bool))}})
    return {k: plan[k] for k in ("version", "name", "project", "scene", "workflow")} | {"jobs": jobs}


def encode_task(task):
    data = (json.dumps(task, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    if not 0 < len(data) <= MAX_TASK_BYTES:
        raise ValueError("Work task definition too large")
    return data


def task_path(work):
    return f"works/{validate_work_id(work)}/task.json"


def work_job(plan, job):
    if not plan.get("work"):
        return None
    return {"id": validate_work_id(plan["work"]["id"]), "item": job["item"], "step": job["step"]}


def media_path(work, suffix):
    if not isinstance(work, dict) or set(work) != {"id", "item", "step"}:
        raise ValueError("Invalid work job")
    identity = validate_work_id(work["id"])
    if not all(isinstance(work[k], str) and re.fullmatch(_COMPONENT, work[k]) for k in ("item", "step")):
        raise ValueError("Invalid work media component")
    suffix = suffix.lower().replace(".jpeg", ".jpg")
    value = f"works/{identity}/{work['item']}/{work['step']}{suffix}"
    if not _MEDIA.fullmatch(value):
        raise ValueError("Unsupported work media extension")
    return value


def parse_media(path):
    match = _MEDIA.fullmatch(path) if isinstance(path, str) else None
    return match.groupdict() if match else None


def derivative_path(original):
    match = parse_media(original)
    if not match:
        return None
    label = "poster" if match["ext"] == "mp4" else "preview"
    return str(PurePosixPath(original).with_suffix(f".{label}.webp"))


def validate_task(work, task):
    identity = validate_work_id(work)
    if (not isinstance(task, dict)
            or task.get("name") != identity.rsplit("--", 1)[0]
            or not isinstance(task.get("jobs"), list) or not task["jobs"]):
        raise ValueError("Invalid work task definition")
    keys = set()
    for job in task["jobs"]:
        if not isinstance(job, dict):
            raise ValueError("Invalid work step")
        item, step = job.get("item"), job.get("step")
        media_path({"id": identity, "item": item, "step": step}, ".png")
        key = f"{step}/{item}"
        if (key in keys or job.get("key") != key
                or not isinstance(job.get("inputs"), dict)
                or not isinstance(job.get("params"), dict)):
            raise ValueError("Invalid or duplicate work step")
        keys.add(key)
    encode_task(task)
    return task


def publish_task(provider, work, task, workspace):
    data = encode_task(validate_task(work, task))
    local = workspace / "task.json"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(data)
    return provider.publish(local, task_path(work), "sha256",
                            expected_sha256=hashlib.sha256(data).hexdigest())


def check_original_slot(provider, work, target):
    from huggingface_hub import list_bucket_tree
    from huggingface_hub.errors import EntryNotFoundError
    prefix = str(PurePosixPath(target).parent)
    try:
        for entry in list_bucket_tree(provider.bucket, prefix=prefix, recursive=False,
                                      token=provider.token):
            parsed = parse_media(entry.path)
            if (getattr(entry, "type", None) == "file" and parsed
                    and parsed["work"] == work["id"] and parsed["item"] == work["item"]
                    and parsed["step"] == work["step"] and entry.path != target):
                raise ValueError("Another original encoding already exists for this work step")
    except EntryNotFoundError:
        pass


def copy_verified(provider, source, target, size, digest):
    """Historical migration only: copy on HF, independently verify destination."""
    from huggingface_hub import batch_bucket_files, get_bucket_file_metadata

    public_url(provider.bucket, source)
    public_url(provider.bucket, target)
    provider.verify_digest(source, size, digest)
    if source == target:
        return "reused"
    try:
        provider.verify_digest(target, size, digest)
        return "reused"
    except HTTPError as exc:
        if exc.code != 404:
            raise
    meta = get_bucket_file_metadata(provider.bucket, source, token=provider.token)
    if meta.size != size or not meta.xet_file_data or not meta.xet_file_data.file_hash:
        raise ValueError("Source Xet identity unavailable")
    try:
        batch_bucket_files(provider.bucket,
                           copy=[("bucket", provider.bucket, meta.xet_file_data.file_hash, target)],
                           token=provider.token)
    except Exception as copy_error:
        # A lost response can still mean success. Preserve the provider error
        # if the destination is positively missing; never ignore a mismatch.
        try:
            provider.verify_digest(target, size, digest)
        except HTTPError as verify_error:
            if verify_error.code == 404:
                raise copy_error from verify_error
            raise
        return "copied"
    provider.verify_digest(target, size, digest)
    return "copied"
