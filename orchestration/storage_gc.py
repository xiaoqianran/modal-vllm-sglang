"""Offline, dependency-aware collection of explicitly managed workflow inputs.

Legacy CAS, model-results, model caches and all execution evidence are never
deletion candidates. A managed input must have at least one verified retired
root, with no remaining live root. Orphans without a published root are kept.
"""
import re
import time

from orchestration import workflow_recovery as recovery
from orchestration.input_store import MANAGED_PREFIX, LIFECYCLE, _verify
from runtime.config.storage import volume_key
from runtime.tasks.artifacts import image_reference
from runtime.tasks.job_client import remote_record, check_record
from runtime.tasks.task_identity import intent_digest

SCHEMA = 1
DEFAULT_AGE = 86400
HEX = re.compile(r"[0-9a-f]{64}")


def _entries(volume, prefix):
    key = volume_key("/" + prefix).lstrip("/")
    entries = []
    try:
        for entry in volume.iterdir(key, recursive=False):
            path = entry.path.lstrip("/")
            if path == key and int(entry.type) == 2:
                continue
            if not path.startswith(key + "/"):
                raise ValueError("Volume listing escaped requested GC namespace")
            entries.append({"path": path[len(key) + 1:], "type": int(entry.type),
                            "bytes": entry.size, "mtime": entry.mtime})
    except FileNotFoundError:
        if entries:
            raise RuntimeError("Volume listing disappeared during GC scan")
    entries.sort(key=lambda e: e["path"])
    if len({e["path"] for e in entries}) != len(entries):
        raise ValueError("Duplicate GC listing entry")
    return entries


def _refs(manifest):
    """Track exact paths, not hashes shared with the protected legacy namespace."""
    refs = {}
    for job in manifest["payload"]["plan"]["jobs"]:
        for field in ("first_frame", "last_frame"):
            binding = job["inputs"].get(field)
            if not isinstance(binding, dict) or "job" in binding:
                continue
            ref = image_reference({k: binding[k] for k in ("path", "sha256", "size")})
            previous = refs.setdefault(ref["path"], {"ref": ref, "jobs": []})
            if previous["ref"] != ref:
                raise ValueError("Conflicting input reference in one workflow")
            previous["jobs"].append({"job": job["key"], "field": field})
    return refs


def _retirement(manifest, checkpoint, owner, proof):
    if (manifest["payload"].get("input_lifecycle") != LIFECYCLE
            or checkpoint is None or checkpoint["status"] != "completed"
            or owner is None or proof is None):
        raise ValueError("Only a completed managed workflow with terminal owner may release inputs")
    return {"schema": SCHEMA, "identity": manifest["identity"], "volume_id": manifest["volume_id"],
            "payload_hash": manifest["payload_hash"], "checkpoint_hash": intent_digest(checkpoint),
            "owner_hash": intent_digest(owner), "terminal_hash": intent_digest(proof),
            "action": "release-managed-inputs"}


def verify_retired(volume, manifest, marker):
    checkpoint = recovery.read_state(volume, manifest)
    owner = recovery.latest_owner(volume, manifest)
    proof = recovery.terminal_evidence(volume, manifest, owner) if owner else None
    if marker != _retirement(manifest, checkpoint, owner, proof):
        raise ValueError("Workflow retirement evidence mismatch")
    return marker


def retire(volume, manifest, modal_api, *, confirm=False):
    """Irreversible release of input roots; completed result delivery remains."""
    if not confirm:
        raise ValueError("Explicit release-inputs confirmation required")
    existing = recovery.read_retirement(volume, manifest)
    if existing is not None:
        return verify_retired(volume, manifest, existing)
    checkpoint = recovery.read_state(volume, manifest)
    if manifest["payload"].get("input_lifecycle") != LIFECYCLE or not checkpoint or checkpoint["status"] != "completed":
        raise ValueError("Only completed managed workflows may release inputs")
    owner = recovery.latest_owner(volume, manifest)
    if owner is None:
        raise ValueError("Completed workflow has no durable owner")
    proof = recovery.seal_terminal(volume, manifest, modal_api, owner)
    if proof is None:
        raise ValueError("Controller termination not proven")
    # Read authoritative model result receipts; never download generated media
    # during GC planning or retirement. Outputs themselves are never deleted.
    for record in checkpoint["jobs"].values():
        receipt = remote_record(volume, record["prefix"], record["id"])
        if not receipt or receipt.get("status") != "completed":
            raise ValueError("Completed GPU receipt missing")
        check_record(record, receipt)
        if any(receipt.get(k) != record["result"].get(k)
               for k in ("id", "file", "sha256", "bytes")):
            raise ValueError("Completed GPU receipt differs from checkpoint")
    marker = _retirement(manifest, checkpoint, owner, proof)
    path = recovery.retirement_path(manifest["identity"])
    if not recovery._put_once(volume, path, marker):
        return verify_retired(volume, manifest, recovery.read_retirement(volume, manifest))
    return marker


def plan_gc(volume, volume_name, *, min_age=DEFAULT_AGE, as_of=None):
    if type(min_age) is not int or min_age < 0:
        raise ValueError("Minimum age must be a nonnegative integer")
    as_of = int(time.time()) if as_of is None else as_of
    if type(as_of) is not int or as_of < 0:
        raise ValueError("Invalid GC snapshot timestamp")
    roots, refs, errors, legacy = [], {}, [], []
    # All cloud roots are discovered via SDK listing, not a local workspace.
    for entry in _entries(volume, "workflow-runs"):
        identity = entry["path"]
        if entry["type"] != 2 or not HEX.fullmatch(identity):
            errors.append({"root": identity, "error": "Unrecognized workflow root"})
            continue
        try:
            manifest = recovery.load_plan(volume, identity, volume_name)
        except FileNotFoundError:
            # Pre-protocol directories can contain only the original state.
            # A damaged managed root with missing plan/owners must block GC.
            children = _entries(volume, "workflow-runs/" + identity)
            if len(children) == 1 and children[0]["path"] == "state.json" and children[0]["type"] == 1:
                legacy.append(identity)
            else:
                errors.append({"root": identity, "error": "Missing durable plan in nonlegacy root"})
            continue
        except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
            errors.append({"root": identity, "error": str(exc)})
            continue
        try:
            marker = recovery.read_retirement(volume, manifest)
            if marker is not None:
                verify_retired(volume, manifest, marker)
            root_refs = _refs(manifest)
            roots.append({"id": identity, "payload_hash": manifest["payload_hash"],
                          "lifecycle": manifest["payload"].get("input_lifecycle", "legacy"),
                          "retirement": intent_digest(marker) if marker else None,
                          "references": root_refs})
            for path, info in root_refs.items():
                ref = info["ref"]
                node = refs.setdefault(path, {"ref": ref, "live": [], "retired": []})
                if node["ref"] != ref:
                    raise ValueError("Conflicting cloud input size or digest")
                # Retirement releases only the new managed namespace. All old
                # CAS and external result references remain permanent roots.
                released = marker is not None and path.startswith(MANAGED_PREFIX)
                node["retired" if released else "live"].append(identity)
        except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
            errors.append({"root": identity, "error": str(exc)})
    objects, candidates, seen = [], [], set()
    for entry in _entries(volume, MANAGED_PREFIX.rstrip("/")):
        path = MANAGED_PREFIX + entry["path"]
        digest = entry["path"]
        if entry["type"] != 1 or not HEX.fullmatch(digest):
            errors.append({"path": path, "error": "Unrecognized managed input; never delete"})
            continue
        try:
            ref = image_reference({"path": path, "sha256": digest, "size": entry["bytes"]})
            if type(entry["mtime"]) is not int or entry["mtime"] < 0:
                raise ValueError("Invalid object modification time")
        except ValueError as exc:
            errors.append({"path": path, "error": str(exc)})
            continue
        seen.add(path)
        node = refs.get(path)
        if node and node["ref"] != ref:
            errors.append({"path": path, "error": "Managed object does not match reference"})
        live, retired = (node["live"], node["retired"]) if node else ([], [])
        if live:
            status = "live"
        elif not retired:
            status = "untracked_protected"
        elif entry["mtime"] > as_of - min_age:
            status = "retired_grace_period"
        else:
            status = "retired_only"
        obj = {**ref, "mtime": entry["mtime"], "live": sorted(live),
               "retired": sorted(retired), "status": status}
        objects.append(obj)
        if status == "retired_only":
            candidates.append(obj)
    # Missing released inputs are expected after a successful collection.
    for path, node in sorted(refs.items()):
        if path.startswith(MANAGED_PREFIX) and node["live"] and path not in seen:
            errors.append({"path": path, "error": "Live managed input missing"})
    result = {"schema": SCHEMA, "volume": volume_name, "volume_id": volume.object_id,
              "scope": MANAGED_PREFIX, "as_of": as_of, "min_age": min_age,
              "roots": roots, "legacy_state_only_roots": legacy,
              "protected_references": {p: n for p, n in sorted(refs.items()) if not p.startswith(MANAGED_PREFIX)},
              "objects": objects, "errors": errors, "candidates": candidates if not errors else [],
              "deletion_allowed": not errors,
              "candidate_bytes": sum(obj["size"] for obj in candidates) if not errors else 0}
    result["snapshot_hash"] = intent_digest(result)
    return result


def apply_gc(volume, volume_name, plan, *, confirm_quiescent=False):
    if not confirm_quiescent:
        raise ValueError("GC requires exclusive offline maintenance, stop all readers/writers")
    if (not isinstance(plan, dict) or plan.get("schema") != SCHEMA
            or plan.get("volume") != volume_name or plan.get("volume_id") != volume.object_id
            or plan.get("scope") != MANAGED_PREFIX
            or plan.get("snapshot_hash") != intent_digest({k: v for k, v in plan.items() if k != "snapshot_hash"})
            or not plan.get("deletion_allowed") or plan.get("errors")):
        raise ValueError("Invalid or blocked GC plan")
    if plan["as_of"] > int(time.time()):
        raise ValueError("GC snapshot is from the future")
    fresh = plan_gc(volume, volume_name, min_age=plan["min_age"], as_of=plan["as_of"])
    if fresh != plan:
        raise RuntimeError("GC snapshot changed; no files deleted, create a new plan")
    # Verify every candidate before the first destructive operation. Only the
    # small managed input files are read. No directory/recursive delete ever.
    for candidate in fresh["candidates"]:
        ref = {k: candidate[k] for k in ("path", "sha256", "size")}
        if (ref["path"] != MANAGED_PREFIX + ref["sha256"]
                or candidate["live"] or not candidate["retired"]):
            raise ValueError("Unsafe GC candidate")
        _verify(volume.read_file(volume_key("/" + ref["path"])), ref)
    if fresh["candidates"] and plan_gc(volume, volume_name, min_age=plan["min_age"], as_of=plan["as_of"]) != fresh:
        raise RuntimeError("GC snapshot changed during verification; no files deleted")
    deleted = []
    for candidate in fresh["candidates"]:
        volume.remove_file(volume_key("/" + candidate["path"]), recursive=False)
        deleted.append({"path": candidate["path"], "size": candidate["size"]})
    return {"volume": volume_name, "snapshot_hash": plan["snapshot_hash"], "deleted": deleted,
            "deleted_bytes": sum(obj["size"] for obj in deleted)}
