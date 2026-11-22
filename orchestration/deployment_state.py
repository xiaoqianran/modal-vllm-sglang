"""Deployment dependency versions and durable preparation diagnostics."""
import ast
from contextlib import contextmanager
from pathlib import Path
import time

from runtime.config.model_spec import ROOT
from runtime.tasks.task_identity import intent_digest
from runtime.utils.files import atomic_json, read_json, sha256_file


def dependency_files(root, modules):
    """Follow local imports, including lazy imports, without importing GPU libraries."""
    root = Path(root)
    pending, found = list(modules), set()
    while pending:
        module = pending.pop()
        path = root.joinpath(*module.split(".")).with_suffix(".py")
        if not path.is_file():
            path = root.joinpath(*module.split(".")) / "__init__.py"
        if not path.is_file() or path in found:
            continue
        found.add(path)
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                pending.extend(alias.name for alias in node.names if alias.name.startswith(("runtime.", "orchestration.", "pipeline.")))
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    package = module.split(".")[:-node.level]
                    base = ".".join(package + ([base] if base else []))
                if base.startswith(("runtime", "orchestration", "pipeline")):
                    pending.append(base)
                    pending.extend(base + "." + alias.name for alias in node.names if alias.name != "*")
    return found


def digest_files(root, paths):
    return intent_digest([(path.relative_to(root).as_posix(), sha256_file(path)) for path in sorted(paths)])


def source_version(root=ROOT):
    root = Path(root)
    paths = dependency_files(root, ["pipeline.deploy_shot", "pipeline.s03_run_shot", "orchestration.sdk", "orchestration.protocol", "orchestration.state"])
    for config in root.glob("models/*/model.json"):
        paths.add(config)
        validator = read_json(config).get("validator_module")
        if validator:
            paths.update(dependency_files(root, [validator]))
    return digest_files(root, paths)


def model_source_version(spec, root=ROOT):
    root = Path(root)
    matches = [path for path in root.glob("models/*/model.json") if read_json(path).get("service") == spec["service"]]
    if len(matches) != 1:
        raise ValueError("Expected one model.json for " + spec["service"])
    runtime = "runtime.serving.model_runtime" if (root / "runtime/serving/model_runtime.py").exists() else "runtime.serving.legacy_cache_preparer"
    validator = spec.get("validator_module")
    paths = dependency_files(root, [runtime, "runtime.serving.inference_gateway",
                                     *([validator] if validator else [])])
    entry = root / "orchestration/provision_entry.py"
    if entry.is_file():
        paths.add(entry)
    return digest_files(root, paths | {matches[0]})


def classify_error(error, stage=None):
    name, text = type(error).__name__, str(error)
    if "Deployment version verification failed" in text:
        # Deployment was already requested. Blind replay can interrupt other
        # jobs and create repeated remote versions; require explicit review.
        return {"error_code": "deployment_verification", "retryable": False, "failed_stage": stage}
    if isinstance(error, ValueError) or "configuration" in text.lower() or "Sources changed" in text:
        return {"error_code": "configuration", "retryable": False, "failed_stage": stage}
    if any(word in name.lower() for word in ("auth", "permission", "secret", "invalid")):
        return {"error_code": "credentials", "retryable": False, "failed_stage": stage}
    code = {"weights": "weights", "cache_check": "cache", "build_deploy": "deployment", "prepare_service": "preparer_deployment"}.get(stage, "control_plane")
    return {"error_code": code, "retryable": True, "failed_stage": stage}


class DeploymentState:
    def __init__(self, root, registry):
        self.root, self.registry = Path(root), registry

    def publish(self, key, value, spec):
        service = spec["service"] if spec is not None else "orchestration"
        path = self.root / ".runtime/models" / (service + ".json")
        previous = read_json(path) if path.exists() else {}
        previous = previous if previous.get("key") == key else {}
        record = {**previous, **value, "key": key, "updated_at": time.time()}
        record.setdefault("started_at", record["updated_at"])
        if value.get("status") in {"ready", "failed"}:
            record["finished_at"] = record["updated_at"]
        elif value.get("status") == "preparing":
            record.pop("finished_at", None)
        self.registry[key] = record
        atomic_json(path, record)

    @contextmanager
    def phase(self, key, spec, stage):
        started, monotonic = time.time(), time.monotonic()
        self.publish(key, {"status": stage, "active_stage": {"stage": stage, "started_at": started}}, spec)
        status = "completed"
        try:
            yield
        except BaseException as exc:
            status = "failed"
            self.publish(key, {"last_failed_stage": stage, "error_type": type(exc).__name__}, spec)
            raise
        finally:
            previous = self.registry.get(key, {})
            stages = previous.get("stages", []) + [{"stage": stage, "status": status, "started_at": started,
                       "finished_at": time.time(), "duration_ms": max(0, (time.monotonic() - monotonic) * 1000)}]
            self.publish(key, {"stages": stages, "active_stage": None}, spec)
