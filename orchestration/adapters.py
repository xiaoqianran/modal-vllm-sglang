"""Model-specific validation and thin wrappers over the existing HTTP clients."""
import base64
import copy
import re

from shared.inference_api import SGLangVideoClient, VLLMImageClient
from shared.minimax_params import parameters as minimax_parameters
from orchestration.state import image_format, local_path, valid

VERSION = 1
CAPABILITIES = {
    "minimax.t2i": {"service": "minimax", "required": {"prompt": "string"}, "output": "image"},
    "moss.t2i": {"service": "moss", "required": {"prompt": "string"}, "output": "image"},
    "pika.t2va": {"service": "pika", "required": {"prompt": "string"}, "output": "video"},
    "pika.fl2va": {"service": "pika", "required": {
        "prompt": "string", "first_frame": "image", "last_frame": "image"}, "output": "video"},
}


def integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def parameters(uses, supplied):
    if uses not in CAPABILITIES:
        raise ValueError(f"Unknown capability: {uses}")
    if not isinstance(supplied, dict):
        raise ValueError("params must be an object")
    if uses == "minimax.t2i":
        return minimax_parameters(supplied)
    if uses == "moss.t2i":
        result = {"model": "OpenMOSS-Team/MOSS-Image-SFT", "size": "1024x1024", "n": 1,
                  "num_inference_steps": 50, "guidance_scale": 3.0, "flow_shift": 5.0,
                  "response_format": "b64_json", "extra_params": {
                      "uncond_skip_layers": [22], "slg_start": 0.01, "slg_stop": 1.0}}
        allowed = {"size", "num_inference_steps", "guidance_scale", "flow_shift"}
    else:
        result = {"model": "Pika-Labs/Pika-H3-Flex", "task": uses.split(".")[1],
                  "seconds": 5, "num_inference_steps": 6, "num_outputs_per_prompt": 1,
                  "target": {"short_edge": 480, "aspect_ratio": "auto", "duration_seconds": 5.0},
                  "conditions": []}
        allowed = {"seconds", "num_inference_steps", "target"}
    unknown = supplied.keys() - allowed
    if unknown:
        raise ValueError(f"Unsupported parameters for {uses}: {sorted(unknown)}")
    result.update(copy.deepcopy(supplied))
    integer(result["num_inference_steps"], "num_inference_steps", 1)
    if uses == "moss.t2i":
        if not isinstance(result["size"], str) or not re.fullmatch(r"[1-9][0-9]*x[1-9][0-9]*", result["size"]):
            raise ValueError("size must be WIDTHxHEIGHT")
        for key in ("guidance_scale", "flow_shift"):
            if type(result[key]) not in (int, float) or not 0 < result[key] < 1000:
                raise ValueError(f"Invalid {key}")
    else:
        integer(result["seconds"], "seconds", 5)
        if result["seconds"] > 15 or result["num_inference_steps"] != 6:
            raise ValueError("Pika requires 5-15 seconds and exactly 6 steps")
        target = result["target"]
        if not isinstance(target, dict) or set(target) != {"short_edge", "aspect_ratio", "duration_seconds"}:
            raise ValueError("target requires short_edge, aspect_ratio, duration_seconds")
        integer(target["short_edge"], "short_edge", 1)
        if not isinstance(target["aspect_ratio"], str) or not re.fullmatch(r"auto|[1-9][0-9]*:[1-9][0-9]*", target["aspect_ratio"]):
            raise ValueError("Invalid aspect_ratio")
        if type(target["duration_seconds"]) not in (int, float) or target["duration_seconds"] != result["seconds"]:
            raise ValueError("target.duration_seconds must equal seconds")
    return result


def frame_bytes(root, artifact):
    if not valid(root, artifact) or artifact.get("type") != "image":
        raise ValueError("Input artifact is missing, changed, or not an image")
    path = local_path(root, artifact["path"])
    if not 0 < path.stat().st_size <= 20 * 1024 * 1024:
        raise ValueError("Frame must be 1 byte to 20 MiB")
    data = path.read_bytes()
    image_format(data)
    return data


class Adapter:
    def __init__(self, uses, endpoint, headers):
        self.uses = uses
        self.output = CAPABILITIES[uses]["output"]
        self.client = (VLLMImageClient if self.output == "image" else SGLangVideoClient)(
            endpoint, headers=headers)

    def prepare(self, params, inputs, root):
        payload = copy.deepcopy(params)
        payload.update(prompt=inputs["prompt"], seed=inputs["seed"])
        for field in ("first_frame", "last_frame"):
            if field in inputs:
                payload[field + "_base64"] = base64.b64encode(frame_bytes(root, inputs[field])).decode("ascii")
        return payload

    def submit(self, payload, timeout):
        if self.output == "image":
            data, _ = self.client.generate(payload, timeout=timeout)
            return {"data": data}
        return {"job_id": self.client.submit(payload, timeout=timeout)}

    def poll(self, job_id, timeout):
        return self.client.status(job_id, timeout=timeout, attempts=1)["status"]

    def collect(self, job_id, timeout):
        return self.client.content(job_id, timeout=timeout, attempts=1)
