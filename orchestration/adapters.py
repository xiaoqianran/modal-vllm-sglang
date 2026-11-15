"""Discovered model capabilities and parameter validation."""
from shared.config.model_spec import discover, parameters as spec_parameters

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
