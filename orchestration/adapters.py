"""Model-specific validation and thin wrappers over the existing HTTP clients."""
import base64
import copy

from shared.inference_api import SGLangVideoClient, VLLMImageClient
from shared.model_spec import discover, parameters as spec_parameters
from orchestration.state import image_format, local_path, valid

VERSION = 2
MODEL_SPECS = discover()
CAPABILITIES = {key: {"service": spec["service"], "required": spec["inputs"], "output": spec["kind"]}
                for key, spec in MODEL_SPECS.items()}


def integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def parameters(uses, supplied):
    if uses not in MODEL_SPECS:
        raise ValueError(f"Unknown capability: {uses}")
    return spec_parameters(MODEL_SPECS[uses], supplied)


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
