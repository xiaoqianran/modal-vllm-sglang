"""Inference item validation and identity; no deployment, network or Modal dependency."""
from pathlib import Path

from shared.tasks.batch import intent_digest
from shared.utils.media import decode_frame
from shared.config.model_spec import parameters, operation_spec
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


def result_prefix(spec, project, scene, operation):
    return f"model-results/{spec['service']}/{project}/{scene}/{operation}-{intent_digest(spec)[:20]}"


def execution_signature(signature, cache_fingerprint):
    return intent_digest({"intent": signature, "cache": cache_fingerprint})
