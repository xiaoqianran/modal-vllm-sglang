"""Local task parsing and identity; no deployment, network or Modal dependency."""
import base64
import hashlib
from pathlib import Path

from shared.tasks.batch import intent_digest
from shared.utils.files import read_json
from shared.utils.media import MAX_FRAME, decode_frame, image_format
from shared.config.model_spec import parameters, operation_spec, operations
from shared.utils.outputs import component
from shared.tasks.artifacts import image_reference

ROOT = Path(__file__).resolve().parents[2]


def prepare(spec, item):
    component(item["id"])
    if not isinstance(item.get("prompt"), str) or not item["prompt"].strip():
        raise ValueError("Nonempty prompt required")
    if type(item.get("seed")) is not int or not 0 <= item["seed"] < 2**32:
        raise ValueError("uint32 seed required")
    operation = item.get("operation", spec["operation"])
    selected = operation_spec(spec, operation)
    payload = parameters(spec, item.get("params", {}), operation)
    payload.update(prompt=item["prompt"], seed=item["seed"])
    for field in ("first_frame", "last_frame"):
        value = item.get(field + "_base64")
        artifact = item.get(field + "_artifact")
        required = field in selected["inputs"]
        if value is not None and artifact is not None:
            raise ValueError("Provide inline image or cloud artifact, not both")
        if required != (value is not None or artifact is not None):
            raise ValueError(f"{field} does not match operation {operation}")
        if artifact is not None:
            payload[field + "_artifact"] = image_reference(artifact)
        elif required:
            decode_frame(value)
            payload[field + "_base64"] = value
    return payload, intent_digest({"spec": spec, "operation": operation, "payload": payload})


def load_task(spec, path, *, root=ROOT, limit=0):
    path = Path(path)
    task = read_json(path if path.is_absolute() else Path(root) / path)
    allowed = {"version", "name", "project", "scene", "workflow", "params", "items"}
    if not isinstance(task, dict) or task.keys() - allowed or type(task.get("version")) is not int or task["version"] != 1:
        raise ValueError("Expected version-1 task JSON")
    component(task["name"])
    project, scene = component(task.get("project", task["name"])), component(task.get("scene", "001-main"))
    candidates = [op for op in operations(spec) if task.get("workflow") == spec["service"] + "-" + op]
    if len(candidates) != 1:
        raise ValueError("Task workflow does not belong to this model")
    operation = candidates[0]
    selected = operation_spec(spec, operation)
    if not isinstance(task.get("params", {}), dict) or not isinstance(task.get("items"), list) or not task["items"]:
        raise ValueError("Task requires params object and nonempty items")
    items, seen = [], set()
    for raw in task["items"]:
        if not isinstance(raw, dict) or raw.keys() - {"id", "prompt", "seed", "params", "first_frame", "last_frame"}:
            raise ValueError("Unsupported item fields")
        shot = component(raw["id"])
        if shot.lower() in seen:
            raise ValueError("Duplicate shot ID")
        seen.add(shot.lower())
        if not isinstance(raw.get("params", {}), dict):
            raise ValueError("Item params must be an object")
        seed = int.from_bytes(hashlib.sha256(f"{task['name']}/{operation}/{shot}".encode()).digest()[:4], "big") & 0x7fffffff
        item = {"id": shot, "operation": operation, "prompt": raw["prompt"], "seed": raw.get("seed", seed),
                "params": {**task.get("params", {}), **raw.get("params", {})}}
        for field in ("first_frame", "last_frame"):
            required = field in selected["inputs"]
            if required != (field in raw):
                raise ValueError(f"{field} does not match operation")
            if required:
                binding = raw[field]
                if not isinstance(binding, dict) or set(binding) != {"file"}:
                    raise ValueError("Frame input must be {file: repo-relative-path}")
                if not isinstance(binding["file"], str) or Path(binding["file"]).is_absolute():
                    raise ValueError("Frame path must be repository relative")
                from shared.utils.files import local_path
                # Input identity is its content, never its local path.
                frame = local_path(root, binding["file"])
                if not frame.is_file() or not 0 < frame.stat().st_size <= MAX_FRAME:
                    raise ValueError("Frame must be 1 byte to 20 MiB")
                data = frame.read_bytes()
                image_format(data)
                item[field + "_base64"] = base64.b64encode(data).decode("ascii")
        prepare(spec, item)  # Validate the full task, even when --limit selects fewer items.
        items.append(item)
    return project, scene, operation, items[:limit] if limit else items


def task_key(project, scene, shot, kind):
    return f"{project}/{scene}/{shot}/{kind}".lower()


def result_prefix(spec, project, scene, operation):
    return f"model-results/{spec['service']}/{project}/{scene}/{operation}-{intent_digest(spec)[:20]}"


def execution_signature(signature, cache_fingerprint):
    return intent_digest({"intent": signature, "cache": cache_fingerprint})
