"""MiniMax H3 image generation parameters, shared by HTTP and deployed SDK."""
import re

MODEL = "MiniMaxAI/MiniMax-H3-Image"
DEFAULTS = {
    "model": MODEL, "size": "2048x2048",
    "num_inference_steps": 6,
    "generator_device": "cpu", "response_format": "b64_json",
}


def parameters(supplied=None):
    if supplied is None:
        supplied = {}
    if not isinstance(supplied, dict):
        raise ValueError("MiniMax params must be an object")
    unknown = supplied.keys() - {"size", "num_inference_steps"}
    if unknown:
        raise ValueError(f"Unsupported MiniMax options: {sorted(unknown)}")
    result = dict(DEFAULTS)
    result.update(supplied)
    if type(result["num_inference_steps"]) is not int or result["num_inference_steps"] != 6:
        raise ValueError("MiniMax H3 Image requires exactly 6 inference steps")
    size = result["size"]
    if not isinstance(size, str) or not re.fullmatch(r"[1-9][0-9]*x[1-9][0-9]*", size):
        raise ValueError("size must be WIDTHxHEIGHT")
    width, height = map(int, size.split("x"))
    if width % 32 or height % 32 or not 4194304 <= width * height <= 20971520:
        raise ValueError("MiniMax size must be 32-pixel aligned, between 4 and 20 MP")
    return result
