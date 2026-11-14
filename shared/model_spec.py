"""One declarative contract for deployment, SDK and HTTP orchestration."""
import copy
import importlib
import math
import re
import json
from pathlib import Path

from shared.files import read_json

ROOT = Path(__file__).resolve().parent.parent
IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]*")


def validate_revision(value):
    """Accept a commit, branch or tag; resolve its existence during CPU download."""
    if (not isinstance(value, str) or not value or value.startswith("REPLACE_")
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("revision must be a commit, branch or tag without whitespace or template placeholders")


def operations(spec):
    return spec.get("operations", {spec["operation"]: {}})


def operation_spec(spec, operation=None):
    operation = operation or spec["operation"]
    if operation not in operations(spec):
        raise ValueError(f"Unsupported operation: {operation}")
    override = operations(spec)[operation]
    result = copy.deepcopy(spec)
    result["operation"] = operation
    result["defaults"] = {**result.get("defaults", {}), **override.get("defaults", {})}
    result.setdefault("options", {})
    result["inputs"] = override.get("inputs", {"prompt": "string"})
    return result


def validate(spec):
    required = {"service", "operation", "kind", "model", "revision", "image", "command"}
    if not isinstance(spec, dict) or required - spec.keys():
        raise ValueError(f"Model spec needs {sorted(required)}")
    json.dumps(spec, allow_nan=False)
    spec = copy.deepcopy(spec)
    spec.setdefault("defaults", {})
    spec.setdefault("options", {})
    for key in ("service", "operation"):
        if not isinstance(spec[key], str) or not IDENTIFIER.fullmatch(spec[key]):
            raise ValueError(f"Invalid model {key}")
    if spec["kind"] not in {"image", "video"}:
        raise ValueError("kind must be image or video")
    for key in ("model", "revision", "image"):
        if not isinstance(spec[key], str) or not spec[key].strip():
            raise ValueError(f"Missing {key}")
    validate_revision(spec["revision"])
    if "cache_directory" in spec:
        from pathlib import PurePosixPath
        directory = PurePosixPath(spec["cache_directory"])
        if (not directory.is_absolute() or ".." in directory.parts or "\\" in str(directory)
                or not directory.is_relative_to("/mnt/modal") or str(directory) == "/mnt/modal"):
            raise ValueError("cache_directory must be inside /mnt/modal")
    if not isinstance(spec["command"], list) or not spec["command"] or not all(isinstance(x, str) for x in spec["command"]):
        raise ValueError("command must be a nonempty argv list")
    if not isinstance(spec["defaults"], dict) or not isinstance(spec["options"], dict):
        raise ValueError("defaults/options must be objects")
    if not all(isinstance(rule, dict) for rule in spec["options"].values()):
        raise ValueError("Option rules must be objects")
    if not isinstance(spec.get("cache", {}), dict):
        raise ValueError("cache must be an object")
    if not isinstance(spec.get("operations", {}), dict) or spec["operation"] not in operations(spec):
        raise ValueError("Default operation must be declared")
    if spec.get("frame_transport") not in (None, "keyframes"):
        raise ValueError("Unknown frame transport")
    if spec.get("validator") and not IDENTIFIER.fullmatch(spec["validator"]):
        raise ValueError("Invalid parameter validator")
    for key in ("model", "prompt", "seed", "first_frame", "last_frame"):
        if key in spec["defaults"] or key in spec["options"]:
            raise ValueError(f"{key} belongs to task inputs")
    for key in ("port", "proxy_port"):
        if key in spec and (type(spec[key]) is not int or not 1 <= spec[key] <= 65535):
            raise ValueError(f"Invalid {key}")
    for key in ("cpu", "memory", "timeout", "startup_timeout", "scaledown_window"):
        if key in spec and (type(spec[key]) is not int or spec[key] <= 0):
            raise ValueError(f"{key} must be a positive integer")
    if spec.get("startup_timeout", 3600) <= 180:
        raise ValueError("startup_timeout must leave time for model loading and proxy startup")
    if not isinstance(spec.get("env", {}), dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in spec.get("env", {}).items()):
        raise ValueError("env must map strings to strings")
    if not isinstance(spec.get("build_commands", []), list) or not all(isinstance(v, str) for v in spec.get("build_commands", [])):
        raise ValueError("build_commands must be a string list")
    if spec.get("proxy_port") == spec.get("port", 8000):
        raise ValueError("Proxy and engine must use distinct ports")
    if "proxy_port" not in spec and spec.get("port", 8000) == 65535:
        raise ValueError("Port 65535 requires an explicit proxy_port")
    if spec.get("cache", {}).get("mode", "hf") not in {"hf", "sglang_overlay"}:
        raise ValueError("Unsupported cache preparation mode")
    if spec.get("cache", {}).keys() - {"mode", "allow_patterns", "required_files"}:
        raise ValueError("Unknown cache options")
    components = spec.get("components", {})
    if not isinstance(components, dict):
        raise ValueError("components must be an object")
    for name, config in components.items():
        if not IDENTIFIER.fullmatch(name) or name == "model" or not isinstance(config, dict):
            raise ValueError("Invalid component name/configuration")
        if not all(isinstance(config.get(k), str) and config[k] for k in ("model", "revision")):
            raise ValueError("Each component needs model and revision")
        validate_revision(config["revision"])
    for config in [spec.get("cache", {}), *components.values()]:
        for key in ("allow_patterns", "required_files"):
            if key in config and (not isinstance(config[key], list) or not all(isinstance(p, str) and p and not Path(p).is_absolute() and ".." not in Path(p).parts for p in config[key])):
                raise ValueError(f"{key} must contain relative file patterns")
    for name, config in operations(spec).items():
        if not IDENTIFIER.fullmatch(name) or not isinstance(config, dict):
            raise ValueError("Invalid operation declaration")
        inputs = config.get("inputs", {"prompt": "string"})
        if inputs.get("prompt") != "string" or any(k not in {"prompt", "first_frame", "last_frame"} or v not in {"string", "image"} for k, v in inputs.items()):
            raise ValueError("Unsupported operation inputs")
        parameters(spec, {}, name)
    return spec


def parameters(spec, supplied, operation=None):
    spec = operation_spec(spec, operation)
    if not isinstance(supplied, dict) or supplied.keys() - spec["options"].keys():
        raise ValueError("Unsupported model parameters")
    result = {"model": spec["model"], **copy.deepcopy(spec["defaults"]), **copy.deepcopy(supplied)}
    if spec.get("validator") == "pika" and "seconds" in supplied and "target" not in supplied:
        result["target"]["duration_seconds"] = supplied["seconds"]
    for key, rule in spec["options"].items():
        if key not in result:
            continue
        value = result[key]
        types = {"integer": (int,), "number": (int, float), "string": (str,), "object": (dict,), "boolean": (bool,)}
        if rule.get("type") not in types or type(value) not in types[rule["type"]]:
            raise ValueError(f"Invalid type for {key}")
        if type(value) in (float, int):
            if not math.isfinite(value) or value < rule.get("minimum", -math.inf) or value > rule.get("maximum", math.inf):
                raise ValueError(f"Out of range: {key}")
        if "enum" in rule and value not in rule["enum"]:
            raise ValueError(f"Invalid value for {key}")
        if "pattern" in rule and (not isinstance(value, str) or not re.fullmatch(rule["pattern"], value)):
            raise ValueError(f"Invalid format for {key}")
    if spec.get("validator"):
        importlib.import_module("shared.params_" + spec["validator"]).validate(result)
    return result


def discover(root=ROOT):
    result = {}
    for path in sorted(Path(root).glob("*/model.json")):
        spec = validate(read_json(path))
        for operation in operations(spec):
            key = f"{spec['service']}.{operation}"
            if key in result:
                raise ValueError(f"Duplicate model capability: {key}")
            result[key] = operation_spec(spec, operation)
    return result
