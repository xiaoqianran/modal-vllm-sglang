"""One declarative contract for deployment, SDK and HTTP orchestration."""
import copy
import importlib
import math
import re
import json
from pathlib import Path

from runtime.utils.files import read_json

ROOT = Path(__file__).resolve().parents[2]
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
    directory = spec.get("service_directory")
    if directory is not None and (not isinstance(directory, str)
            or not re.fullmatch(r"[0-9]{3}-[a-z0-9-]+", directory)):
        raise ValueError("Invalid service_directory")
    spec.setdefault("options", {})
    if "metrics_interval" in spec:
        raise ValueError("Use telemetry_interval for telemetry sampling")
    from runtime.config.storage import VOLUME_NAME
    if spec.get("volume_name", VOLUME_NAME) != VOLUME_NAME:
        raise ValueError(f"volume_name must be {VOLUME_NAME}")
    if spec.get("model_storage", "legacy") not in ("legacy", "shared"):
        raise ValueError("model_storage must be legacy or shared")
    if spec.get("model_storage") == "shared" and (
            spec.get("cache", {}).get("mode") == "sglang_overlay"
            or any(c.get("mode") == "sglang_overlay" for c in spec.get("components", {}).values())):
        raise ValueError("SGLang Overlay requires writable materialization storage")
    if spec.get("memory_snapshot", "off") not in ("off", "gpu"):
        raise ValueError("memory_snapshot must be off or gpu")
    if ("download_workers" in spec and
            (type(spec["download_workers"]) is not int
             or not 1 <= spec["download_workers"] <= 8)):
        raise ValueError("download_workers must be an integer between 1 and 8")
    if spec.get("download_policy", "active") not in ("active", "archived"):
        raise ValueError("download_policy must be active or archived")
    for key in ("service", "operation"):
        if not isinstance(spec[key], str) or not IDENTIFIER.fullmatch(spec[key]):
            raise ValueError(f"Invalid model {key}")
    if spec["kind"] not in {"image", "video", "llm"}:
        raise ValueError("kind must be image, video or llm")
    if spec["kind"] == "llm" and spec["operation"] != "chat":
        raise ValueError("LLM only supports chat")
    for key in ("model", "revision", "image"):
        if not isinstance(spec[key], str) or not spec[key].strip():
            raise ValueError(f"Missing {key}")
    validate_revision(spec["revision"])
    if "cache_directory" in spec:
        from pathlib import PurePosixPath
        directory = PurePosixPath(spec["cache_directory"])
        if (not directory.is_absolute() or ".." in directory.parts or "\\" in str(directory)
                or not directory.is_relative_to("/data") or str(directory) == "/data"):
            raise ValueError("cache_directory must be inside /data")
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
    validator = spec.get("validator")
    validator_module = spec.get("validator_module")
    if validator is not None and (not isinstance(validator, str) or not IDENTIFIER.fullmatch(validator)):
        raise ValueError("Invalid parameter validator")
    if bool(validator) != bool(validator_module):
        raise ValueError("Each parameter validator requires an explicit validator_module")
    if validator_module and (not isinstance(validator_module, str)
            or not re.fullmatch(r"models\.[0-9]{3}-[a-z0-9-]+\.[a-z][a-z0-9_]*\.params", validator_module)):
        raise ValueError("Invalid parameter validator_module")
    implementation = spec.get("cache_implementation")
    if implementation is not None:
        if (not isinstance(implementation, str)
                or not re.fullmatch(r"models\.[0-9]{3}-[a-z0-9-]+\.model_impl", implementation)):
            raise ValueError("Invalid cache_implementation")
        if spec.get("cache", {}).get("mode") != "sglang_overlay":
            raise ValueError("cache_implementation is only for specialized Overlay models")
        if validator_module and not validator_module.startswith(implementation + "."):
            raise ValueError("Cache implementation and validator must belong to the same model")
    for key in ("model", "prompt", "seed", "first_frame", "last_frame"):
        if key in spec["defaults"] or key in spec["options"]:
            raise ValueError(f"{key} belongs to task inputs")
    for key in ("port", "proxy_port"):
        if key in spec and (type(spec[key]) is not int or not 1 <= spec[key] <= 65535):
            raise ValueError(f"Invalid {key}")
    for key in ("cpu", "memory", "timeout", "startup_timeout", "scaledown_window", "max_inputs", "telemetry_interval"):
        if key in spec and (type(spec[key]) is not int or spec[key] <= 0):
            raise ValueError(f"{key} must be a positive integer")
    if spec.get("startup_timeout", 3600) <= 180:
        raise ValueError("startup_timeout must leave time for model loading and proxy startup")
    if not isinstance(spec.get("env", {}), dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in spec.get("env", {}).items()):
        raise ValueError("env must map strings to strings")
    if not isinstance(spec.get("build_commands", []), list) or not all(isinstance(v, str) for v in spec.get("build_commands", [])):
        raise ValueError("build_commands must be a string list")
    if (not isinstance(spec.get("uv_packages", []), list)
            or not all(isinstance(v, str) and v.strip() and not v.startswith("-")
                       for v in spec.get("uv_packages", []))):
        raise ValueError("uv_packages must be a list of nonempty package requirements")
    if spec["image"] == "debian_slim":
        version = spec.get("python_version")
        if not isinstance(version, str) or not re.fullmatch(r"3\.\d{1,2}", version):
            raise ValueError("debian_slim requires an explicit Python minor version, e.g. 3.12")
        if not spec.get("uv_packages"):
            raise ValueError("debian_slim requires uv_packages for the inference engine")
    elif "python_version" in spec:
        raise ValueError("python_version is only supported for debian_slim")
    if "uv_torch_backend" in spec:
        backend = spec["uv_torch_backend"]
        if not spec.get("uv_packages") or not isinstance(backend, str) or not re.fullmatch(r"cu\d{3}", backend):
            raise ValueError("uv_torch_backend requires uv_packages and an explicit CUDA backend, e.g. cu130")
    if "uv_only_binary" in spec:
        if type(spec["uv_only_binary"]) is not bool or not spec.get("uv_packages"):
            raise ValueError("uv_only_binary must be a boolean and requires uv_packages")
    if "uv_extra_index_url" in spec:
        from urllib.parse import urlsplit
        url = spec["uv_extra_index_url"]
        if (not isinstance(url, str) or not spec.get("uv_packages")
                or any(c.isspace() for c in url)):
            raise ValueError("uv_extra_index_url requires uv_packages and an HTTPS index URL")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query:
            raise ValueError("uv_extra_index_url must be an HTTPS index URL without credentials")
    if spec.get("proxy_port") == spec.get("port", 8000):
        raise ValueError("Proxy and engine must use distinct ports")
    if "proxy_port" not in spec and spec.get("port", 8000) == 65535:
        raise ValueError("Port 65535 requires an explicit proxy_port")
    if spec.get("cache", {}).get("mode", "hf") not in {"hf", "sglang_overlay"}:
        raise ValueError("Unsupported cache preparation mode")
    if spec.get("cache", {}).keys() - {"mode", "allow_patterns", "required_files"}:
        raise ValueError("Unknown cache options")
    profiles = spec.get("resolution_profiles", {})
    if not isinstance(profiles, dict):
        raise ValueError("resolution_profiles must map preset names to aspect-ratio canvases")
    for preset, canvases in profiles.items():
        if not isinstance(preset, str) or not isinstance(canvases, dict):
            raise ValueError("Invalid resolution profile")
        for ratio, dims in canvases.items():
            if (not isinstance(ratio, str) or not re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", ratio)
                    or not isinstance(dims, list) or len(dims) != 2
                    or any(type(value) is not int or value <= 0 for value in dims)):
                raise ValueError("Resolution canvas must contain a ratio and positive [width, height]")
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
    if "snapshot_warmup" in spec:
        warmup = spec["snapshot_warmup"]
        if (spec["kind"] != "image"
                or not isinstance(warmup, dict)
                or set(warmup) != {"params"} or not isinstance(warmup["params"], dict)
                or spec["operation"] != "t2i"):
            raise ValueError("snapshot_warmup requires a text-to-image params object")
        parameters(spec, warmup["params"])
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
    validator = (importlib.import_module(spec["validator_module"])
                 if spec.get("validator_module") else None)
    if validator is not None and hasattr(validator, "normalize"):
        validator.normalize(result, supplied)
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
    if validator is not None:
        validator.validate(result)
    return result


def discover(root=ROOT):
    result = {}
    for path in sorted(Path(root).glob("models/*/model.json")):
        spec = validate(read_json(path))
        if spec["kind"] == "llm":
            continue
        for operation in operations(spec):
            key = f"{spec['service']}.{operation}"
            if key in result:
                raise ValueError(f"Duplicate model capability: {key}")
            result[key] = operation_spec(spec, operation)
    return result
