"""Read-only inventory of the protected legacy workflow input namespace.

Local plans are not a complete set of historical cloud dependency roots.
Never label an unobserved object safe to delete, and never access model weights.
"""
from pathlib import Path
import re

from runtime.config.storage import VOLUME_SUB_PATH, volume_key
from runtime.tasks.artifacts import image_reference, IMAGE_FIELDS, image_limit
from runtime.tasks.task_identity import intent_digest
from runtime.utils.files import read_json


def audit(volume, volume_name, roots):
    referenced, explicit, errors = {}, set(), []
    plans = 0
    seen = set()
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            raise ValueError(f"Missing task plan directory: {root}")
        for path in sorted(root.glob("*/*/plan.json")):
            path = path.resolve()
            if path in seen:
                continue
            seen.add(path)
            try:
                plan = read_json(path)
                if not isinstance(plan, dict):
                    raise ValueError("Invalid plan record")
                if plan.get("volume") != volume_name:
                    continue
                if (plan.get("volume_sub_path") != VOLUME_SUB_PATH
                        or plan.get("hash") != intent_digest({k: v for k, v in plan.items() if k != "hash"})):
                    raise ValueError("Plan storage/hash mismatch")
                # A plan covers future as well as active jobs. Its local
                # frozen input might not have been uploaded yet.
                for job in plan["jobs"]:
                    for field in IMAGE_FIELDS:
                        binding = job["inputs"].get(field)
                        if not isinstance(binding, dict) or "job" in binding:
                            continue
                        if binding.get("storage") == "volume":
                            ref = image_reference({k: binding[k] for k in ("path", "sha256", "size")}, max_bytes=image_limit(field))
                            if (not ref["path"].startswith("workflow-inputs/")
                                    or ref["path"].startswith(("workflow-inputs/managed-v1/", "workflow-inputs/preview-originals/"))):
                                continue
                            if ref["path"] != "workflow-inputs/" + ref["sha256"]:
                                raise ValueError("Noncanonical CAS input path")
                            explicit.add(ref["sha256"])
                        else:
                            ref = image_reference({"path": "workflow-inputs/" + binding["sha256"],
                                                   "sha256": binding["sha256"], "size": binding["size"]}, max_bytes=image_limit(field))
                        referenced.setdefault(ref["sha256"], []).append({
                            "plan": str(path), "job": job["key"], "field": field, "size": ref["size"]})
                plans += 1
            except (OSError, ValueError, TypeError, KeyError) as exc:
                errors.append({"plan": str(path), "error": str(exc)})

    prefix = volume_key("/workflow-inputs").lstrip("/") + "/"
    objects, unknown = [], []
    try:
        for entry in volume.iterdir(prefix.rstrip("/"), recursive=False):
            path = entry.path.removeprefix("/")
            # SDK directory listings may return the directory itself.
            if path in {prefix.rstrip("/"), prefix + "managed-v1", prefix + "preview-originals"} and int(entry.type) == 2:
                continue
            digest = path.removeprefix(prefix)
            if (not path.startswith(prefix) or not re.fullmatch("[0-9a-f]{64}", digest)
                    or int(entry.type) != 1 or type(entry.size) is not int or entry.size <= 0):
                unknown.append({"path": path, "bytes": entry.size, "type": int(entry.type)})
                continue
            refs = referenced.get(digest, [])
            if refs and any(ref["size"] != entry.size for ref in refs):
                errors.append({"path": path, "error": "Referenced input size mismatch"})
            objects.append({"path": path, "sha256": digest, "bytes": entry.size,
                            "mtime": entry.mtime, "references": refs,
                            "status": "referenced_by_scanned_plans" if refs else "unobserved_by_scanned_plans"})
    except FileNotFoundError:
        pass  # No input namespace yet.
    objects.sort(key=lambda item: item["path"])
    present = {item["sha256"] for item in objects}
    missing = [{"sha256": key, "references": referenced[key], "expected_in_volume": key in explicit}
               for key in sorted(referenced.keys() - present)]
    return {"schema": 1, "volume": volume_name, "volume_id": volume.object_id,
            "scope": prefix, "plans_scanned": plans, "objects": objects,
            "unknown_entries": unknown, "missing_references": missing, "errors": errors,
            "referenced_bytes": sum(item["bytes"] for item in objects if item["references"]),
            "unobserved_bytes": sum(item["bytes"] for item in objects if not item["references"]),
            "deletion_allowed": False,
            "limitation": "Legacy-only advisory scan; local plans do not cover all cloud submissions. Use gc_workflow_inputs.py plan for managed cloud roots."}
