"""Plan or apply a non-destructive copy of one known historical task.

No media is downloaded to the local machine and no inference is started.
Dry run is the default. SHA-256 verification streams bytes without saving them.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from orchestration.sdk import snapshot
from runtime.archive.huggingface import HuggingFaceBucket, public_url
from runtime.archive.works import (copy_verified, derivative_path, media_path,
                                   publish_task, task_definition, validate_task, work_id)


def _copy(row, bucket, target, *, original):
    path = row["path"]
    digest = row["stored_sha256"] if original else row["sha256"]
    size = row["stored_bytes"] if original else row["bytes"]
    import re
    if (row.get("verification") != "sha256" or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest) or type(size) is not int or size <= 0
            or row.get("url") != public_url(bucket, path)):
        raise ValueError("Unverified historical media")
    return {"source": path, "target": target, "size": size, "sha256": digest}


def migration_plan(plan, current, *, item=None):
    bucket = plan.get("delivery", {}).get("bucket")
    if not bucket:
        raise ValueError("Historical task has no HF delivery destination")
    identity = (plan.get("work") or {}).get("id") or work_id(plan)
    definition = task_definition(plan)
    validate_task(identity, definition)
    copies, missing, seen = [], [], set()
    if item is not None and not any(job["item"] == item for job in plan["jobs"]):
        raise ValueError("Unknown migration item")
    for job in plan["jobs"]:
        if item is not None and job["item"] != item:
            continue
        record = current["jobs"][job["key"]]
        row = record.get("cloud_archive")
        if not row:
            missing.append(job["key"])
            continue
        if row.get("status") != "archived" or row.get("bucket") != bucket:
            raise ValueError("Historical receipt has wrong status or bucket")
        artifact = record.get("artifacts", {}).get(row.get("kind"))
        if (not artifact or artifact.get("sha256") != row.get("original_sha256")
                or artifact.get("size") != row.get("original_bytes")):
            raise ValueError("Historical receipt does not match its generation artifact")
        work = {"id": identity, "item": job["item"], "step": job["step"]}
        target = media_path(work, PurePosixPath(row["path"]).suffix)
        copies.append(_copy(row, bucket, target, original=True))
        derivative = row.get("preview" if row["kind"] == "image" else "poster")
        if derivative is not None:
            from runtime.delivery.outbox import validate_derivative
            name = "preview" if row["kind"] == "image" else "poster"
            validate_derivative({"status": name, "source_sha256": row["stored_sha256"],
                                 name: derivative}, row)
            copies.append(_copy(derivative, bucket, derivative_path(target), original=False))
    for row in copies:
        if row["target"] in seen:
            raise ValueError("Conflicting migration destination")
        seen.add(row["target"])
    return {"id": identity, "bucket": bucket, "task": definition,
            "copies": copies, "missing": missing}


def apply_migration(description, provider):
    if provider.bucket != description["bucket"]:
        raise ValueError("Migration bucket mismatch")
    if not description["copies"]:
        return []
    with TemporaryDirectory(prefix="hf-work-task-") as temporary:
        publish_task(provider, description["id"], description["task"], Path(temporary))
    return [{**row, "status": copy_verified(provider, **row)}
            for row in description["copies"]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="Existing .runtime/tasks/PROJECT/SCENE")
    parser.add_argument("--item", help="Copy only one shot; the full frozen task definition is preserved")
    parser.add_argument("--apply", action="store_true", help="Actually copy; never delete source")
    args = parser.parse_args()
    plan, current = snapshot(args.directory)
    description = migration_plan(plan, current, item=args.item)
    summary = {k: v for k, v in description.items() if k != "task"}
    if args.apply:
        summary["copies"] = apply_migration(description, HuggingFaceBucket(description["bucket"]))
    summary["dry_run"] = not args.apply
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
