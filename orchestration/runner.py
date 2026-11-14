"""Offline planning and sequential, stage-first workflow execution."""
import argparse
import copy
import hashlib
import json
import math
import os
import re
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from shared.batch import intent_digest
from shared.files import read_json, sha256_file
from shared.task_lifecycle import recovery_action, SubmissionRejected
from orchestration import adapters, state

ROOT = Path(__file__).resolve().parent.parent


def identifier(value):
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value)
            or value.upper() in reserved):
        raise ValueError(f"Invalid identifier: {value!r}")
    return value


def fields(obj, required, optional=()):
    if not isinstance(obj, dict) or set(required) - obj.keys() or obj.keys() - set(required) - set(optional):
        raise ValueError(f"Expected fields {sorted(required)}, optional {sorted(optional)}")


def reference(binding):
    fields(binding, {"ref"})
    if not isinstance(binding["ref"], str):
        raise ValueError("ref must be a string")
    parts = binding["ref"].split(".")
    if len(parts) != 2:
        raise ValueError("ref must be input.FIELD or STEP.OUTPUT")
    for part in parts:
        identifier(part)
    return parts


def build_plan(task_path, *, root=ROOT, endpoints_path="endpoints.example.json"):
    """No network, environment lookups, output writes, or GPU startup."""
    root = Path(root).resolve()
    task_path = state.local_path(root, task_path)
    task = read_json(task_path)
    fields(task, {"version", "name", "workflow", "items"}, {"project", "scene", "params"})
    if not isinstance(task.get("params", {}), dict):
        raise ValueError("Task params must be an object")
    if type(task["version"]) is not int or task["version"] != 1:
        raise ValueError("Unsupported task version")
    identifier(task["name"])
    if not re.fullmatch(r"[0-9]{3}(?:-[a-z0-9]+)+", task["name"]) or task_path.stem != task["name"]:
        raise ValueError("Task name must match filename and use NNN-short-description")
    project = identifier(task.get("project", task["name"]))
    scene = identifier(task.get("scene", "001-main"))
    if not re.fullmatch(r"[0-9]{3}(?:-[a-z0-9]+)+", project):
        raise ValueError("Project must use NNN-short-description")
    if not re.fullmatch(r"[0-9]{3}(?:-[a-z0-9]+)+", scene):
        raise ValueError("Scene must use NNN-short-description")
    workflow_id = identifier(task["workflow"])
    workflow = read_json(state.local_path(root, f"workflows/{workflow_id}.json"))
    fields(workflow, {"version", "id", "steps", "outputs"})
    if type(workflow["version"]) is not int or workflow["version"] != 1 or workflow["id"] != workflow_id:
        raise ValueError("Workflow version or id mismatch")
    if not isinstance(workflow["steps"], list) or not workflow["steps"]:
        raise ValueError("Workflow needs steps")
    if len(workflow["steps"]) != 1 and task.get("params"):
        raise ValueError("For multi-step workflows, put params on workflow steps")
    steps = {}
    for original in workflow["steps"]:
        step = copy.deepcopy(original)
        fields(step, {"id", "uses", "inputs"}, {"params"})
        step_id = identifier(step["id"])
        if step_id == "input" or step_id in steps:
            raise ValueError("Duplicate or reserved step id")
        if step["uses"] not in adapters.CAPABILITIES:
            raise ValueError(f"Unknown capability: {step['uses']}")
        cap = adapters.CAPABILITIES[step["uses"]]
        fields(step["inputs"], cap["required"], {"seed"})
        if not isinstance(step.get("params", {}), dict):
            raise ValueError("Step params must be an object")
        step["supplied_params"] = {**step.get("params", {}), **task.get("params", {})}
        step["params"] = adapters.parameters(step["uses"], step["supplied_params"])
        step["deps"] = []
        steps[step_id] = step
    for step in steps.values():
        for field, binding in step["inputs"].items():
            source, output = reference(binding)
            if source == "input":
                continue
            if source not in steps or output != adapters.CAPABILITIES[steps[source]["uses"]]["output"]:
                raise ValueError(f"Unknown output: {binding}")
            expected = adapters.CAPABILITIES[step["uses"]]["required"].get(field, "integer")
            if output != expected:
                raise ValueError(f"Type mismatch for {step['id']}.{field}")
            if source not in step["deps"]:
                step["deps"].append(source)
    ordered = []
    while len(ordered) < len(steps):
        ready = [key for key, step in steps.items() if key not in ordered and all(d in ordered for d in step["deps"])]
        if not ready:
            raise ValueError("Workflow has a dependency cycle")
        ordered.extend(ready)
    if not isinstance(workflow["outputs"], dict) or not workflow["outputs"]:
        raise ValueError("Workflow needs named outputs")
    for name, binding in workflow["outputs"].items():
        identifier(name)
        source, output = reference(binding)
        if source not in steps or output != adapters.CAPABILITIES[steps[source]["uses"]]["output"]:
            raise ValueError("Invalid workflow output")
    if not isinstance(task["items"], list) or not task["items"]:
        raise ValueError("Task needs nonempty items")
    ids = set()
    # Windows paths are case insensitive: reject case-only collisions everywhere.
    if len({s.lower() for s in steps}) != len(steps):
        raise ValueError("Case-colliding step IDs")
    jobs, imports = [], {}
    for item in task["items"]:
        if not isinstance(item, dict):
            raise ValueError("Item must be an object")
        if not isinstance(item.get("params", {}), dict):
            raise ValueError("Item params must be an object")
        if len(steps) != 1 and item.get("params"):
            raise ValueError("For multi-step workflows, put params on workflow steps")
        item_id = identifier(item.get("id"))
        if type(item.get("reuse_existing", False)) is not bool:
            raise ValueError("reuse_existing must be a boolean")
        if item_id.lower() in ids:
            raise ValueError("Duplicate item ID")
        ids.add(item_id.lower())
    for step_id in ordered:
        step = steps[step_id]
        cap = adapters.CAPABILITIES[step["uses"]]
        for item in task["items"]:
            inputs = {}
            for field, binding in step["inputs"].items():
                source, key = reference(binding)
                if source != "input":
                    inputs[field] = {"job": f"{source}/{item['id']}", "output": key}
                    continue
                if key not in item:
                    raise ValueError(f"Missing input.{key} in {item['id']}")
                value = item[key]
                expected = cap["required"].get(field, "integer")
                if expected == "string":
                    if not isinstance(value, str) or not value.strip():
                        raise ValueError(f"{key} must be a nonempty string")
                elif expected == "integer":
                    adapters.integer(value, key)
                else:
                    fields(value, {"file"})
                    path = state.local_path(root, value["file"])
                    if not path.is_file() or not 0 < path.stat().st_size <= 20 * 1024 * 1024:
                        raise ValueError(f"Invalid input image: {path}")
                    _, mime = state.image_format(path.read_bytes())
                    digest = sha256_file(path)
                    value = {"type": "image", "path": f".runner/inputs/{digest}", "sha256": digest,
                             "size": path.stat().st_size, "mime": mime}
                    imports[value["path"]] = {"source": path.relative_to(root).as_posix(), "artifact": value}
                inputs[field] = copy.deepcopy(value)
            if "seed" not in inputs:
                # Stable per identity, independent of item order or batch size.
                seed_scope = step["uses"].split(".")[1] if len(steps) == 1 else step_id
                seed_key = f"{task['name']}/{seed_scope}/{item['id']}".encode()
                inputs["seed"] = int.from_bytes(hashlib.sha256(seed_key).digest()[:4], "big") & 0x7fffffff
                if len(steps) == 1 and "seed" in item:
                    adapters.integer(item["seed"], "seed")
                    if item["seed"] >= 2**32:
                        raise ValueError("seed must be uint32")
                    inputs["seed"] = item["seed"]
            params = adapters.parameters(step["uses"], {**step["supplied_params"], **item.get("params", {})})
            jobs.append({"key": f"{step_id}/{item['id']}", "step": step_id, "item": item["id"],
                         "uses": step["uses"], "params": params, "inputs": inputs,
                         "reuse_existing": item.get("reuse_existing", False)})
    config = read_json(state.local_path(root, endpoints_path))
    fields(config, {"version", "services"})
    if type(config["version"]) is not int or config["version"] != 1 or not isinstance(config["services"], dict):
        raise ValueError("Invalid endpoints configuration")
    services = {}
    for name in sorted({adapters.CAPABILITIES[j["uses"]]["service"] for j in jobs}):
        service = config["services"].get(name)
        fields(service, {"endpoint_env", "revision"}, {"headers_env"})
        identifier(service["endpoint_env"])
        if not isinstance(service["revision"], str) or not service["revision"].strip():
            raise ValueError("Service revision must be nonempty")
        if not isinstance(service.get("headers_env", {}), dict):
            raise ValueError("headers_env must be an object")
        for header, env in service.get("headers_env", {}).items():
            if not re.fullmatch(r"[A-Za-z0-9-]+", header):
                raise ValueError("Invalid header name")
            identifier(env)
        services[name] = copy.deepcopy(service)
    plan = {"version": 1, "layout_version": 3, "adapter_version": adapters.VERSION,
            "name": task["name"], "project": project, "scene": scene,
            "workflow": workflow_id, "jobs": jobs, "services": services,
            "outputs": workflow["outputs"], "imports": imports}
    plan["hash"] = intent_digest(plan)
    return plan


def bindings(plan):
    result = {}
    for name, service in plan["services"].items():
        endpoint = os.environ.get(service["endpoint_env"], "").rstrip("/")
        url = urlsplit(endpoint)
        if url.scheme not in {"http", "https"} or not url.netloc or url.username or url.password or url.query or url.fragment:
            raise ValueError(f"Set {service['endpoint_env']} to a service URL without credentials/query/fragment")
        headers = {}
        for header, env in service.get("headers_env", {}).items():
            value = os.environ.get(env)
            if not value or "\r" in value or "\n" in value:
                raise ValueError(f"Missing or invalid environment variable: {env}")
            headers[header] = value
        result[name] = {"endpoint": endpoint, "headers": headers}
    return result


def _extend_scene(directory, old, plan, saved):
    """Append new shots without resetting completed jobs or changing old inputs."""
    if old.get("layout_version") != 3 or plan.get("layout_version") != 3:
        raise ValueError("Cannot extend an older run; migrate it first")
    if intent_digest({key: value for key, value in old.items() if key != "hash"}) != old.get("hash"):
        raise ValueError("Existing scene plan has been modified")
    for key in ("name", "project", "scene", "workflow", "services", "outputs", "adapter_version"):
        if old.get(key) != plan.get(key):
            raise ValueError(f"Cannot change frozen scene configuration: {key}")
    for key, value in old.get("imports", {}).items():
        if plan.get("imports", {}).get(key) != value:
            raise ValueError(f"Cannot change a frozen imported file: {key}")
    old_jobs = {j["key"]: j for j in old["jobs"]}
    new_jobs = {j["key"]: j for j in plan["jobs"]}
    if not old_jobs.keys() < new_jobs.keys() or any(new_jobs[k] != v for k, v in old_jobs.items()):
        raise ValueError("Scene updates must append new shots; existing shot definitions must be unchanged")
    if saved["plan_hash"] != old["hash"]:
        raise ValueError("Existing scene state hash mismatch")
    journal = directory / ".runner" / "extension.json"
    # The journal allows a later invocation to diagnose an interrupted two-file update.
    state.save(journal, {"old_hash": old["hash"], "new_hash": plan["hash"],
                         "added": sorted(new_jobs.keys() - old_jobs.keys())})
    state.save(directory / "plan.json", plan)
    saved["plan_hash"] = plan["hash"]
    saved["jobs"].update({key: {"status": "pending"} for key in new_jobs.keys() - old_jobs.keys()})
    state.save(directory / "state.json", saved)
    journal.unlink(missing_ok=True)


def new_run(plan, *, root=ROOT, output="outputs"):
    root = Path(root).resolve()
    output = state.local_path(root, output)
    runtime = bindings(plan)  # Fail before writing any Run if credentials are missing.
    with state.lock(output):
        directory = output / plan["project"] / plan["scene"]
        if directory.joinpath("plan.json").exists() or directory.joinpath("state.json").exists():
            if not (directory.joinpath("plan.json").is_file() and directory.joinpath("state.json").is_file()):
                raise ValueError("Incomplete scene state; refusing overwrite")
            old = read_json(directory / "plan.json")
            saved = read_json(directory / "state.json")
            if saved.get("endpoints") != {k: v["endpoint"] for k, v in runtime.items()}:
                raise ValueError("Scene endpoint changed")
            if old != plan:
                if saved.get("plan_hash") != old.get("hash"):
                    raise ValueError("Interrupted scene extension; inspect .runner/extension.json")
                # Persist new imported inputs before exposing the extended plan.
                for relative, entry in plan["imports"].items():
                    if relative not in old.get("imports", {}):
                        data = state.local_path(root, entry["source"]).read_bytes()
                        if hashlib.sha256(data).hexdigest() != entry["artifact"]["sha256"]:
                            raise ValueError("Imported input changed since planning")
                        target = state.local_path(directory, relative)
                        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != entry["artifact"]["sha256"]:
                            raise FileExistsError(f"Conflicting imported input: {target}")
                        if not target.exists():
                            state.write(target, data)
                _extend_scene(directory, old, plan, saved)
            elif saved.get("plan_hash") != plan["hash"]:
                journal = directory / ".runner" / "extension.json"
                note = read_json(journal) if journal.exists() else {}
                if (note.get("old_hash") == saved.get("plan_hash")
                        and note.get("new_hash") == plan["hash"]):
                    saved["jobs"].update({job["key"]: {"status": "pending"}
                                          for job in plan["jobs"] if job["key"] not in saved["jobs"]})
                    saved["plan_hash"] = plan["hash"]
                    state.save(directory / "state.json", saved)
                    journal.unlink(missing_ok=True)
                else:
                    raise ValueError("Scene plan/state mismatch")
            return directory
        directory.mkdir(parents=True, exist_ok=True)
        if any(directory.iterdir()) and not (
            all(p.name in {".runner", ".runner.lock"} or
                (p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".mp4"})
                for p in directory.iterdir())):
            raise ValueError("Scene contains unrecognized files; refusing to initialize")
        for relative, entry in plan["imports"].items():
            data = state.local_path(root, entry["source"]).read_bytes()
            if hashlib.sha256(data).hexdigest() != entry["artifact"]["sha256"]:
                raise ValueError("Input changed since planning; plan again")
            state.write(state.local_path(directory, relative), data)
        state.save(directory / "plan.json", plan)
        state.save(directory / "state.json", {
            "plan_hash": plan["hash"],
            "endpoints": {k: v["endpoint"] for k, v in runtime.items()},
            "jobs": {job["key"]: {"status": "pending"} for job in plan["jobs"]}})
    return directory


def report(plan, records):
    outputs = {}
    for job in plan["jobs"]:
        for name, binding in plan["outputs"].items():
            step, output = reference(binding)
            record = records[job["key"]]
            if step == job["step"] and record["status"] == "completed":
                outputs.setdefault(job["item"], {})[name] = record["artifacts"][output]
    return {"name": plan["name"], "counts": dict(Counter(r["status"] for r in records.values())),
            "outputs": outputs}


def execute(directory, *, adapter_factory=adapters.Adapter, timeout=3600, poll=2):
    """Resume frozen plan. Never resubmit a record that left pending/blocked."""
    if not math.isfinite(timeout) or not math.isfinite(poll) or timeout <= 0 or poll <= 0:
        raise ValueError("timeout and poll must be positive")
    directory = Path(directory).resolve()
    # Scene directory is outputs/project/scene. No run-id directory.
    with state.lock(directory.parent.parent), state.lock(directory):
        plan = read_json(directory / "plan.json")
        frozen = {k: v for k, v in plan.items() if k != "hash"}
        if intent_digest(frozen) != plan["hash"] or plan["adapter_version"] != adapters.VERSION:
            raise ValueError("Plan modified or adapter version changed; cannot resume")
        current = read_json(directory / "state.json")
        if current["plan_hash"] != plan["hash"]:
            raise ValueError("State does not match plan")
        runtime = bindings(plan)
        if current["endpoints"] != {k: v["endpoint"] for k, v in runtime.items()}:
            raise ValueError("Endpoint changed; refusing to query/resubmit on another service")
        records = current["jobs"]

        def persist():
            state.save(directory / "state.json", current)

        for job in plan["jobs"]:
            record = records[job["key"]]
            inputs = copy.deepcopy(job["inputs"])
            blocked = False
            for field, value in inputs.items():
                if isinstance(value, dict) and "job" in value:
                    upstream = records[value["job"]]
                    artifact = upstream.get("artifacts", {}).get(value["output"])
                    if upstream["status"] != "completed" or not state.valid(directory, artifact):
                        blocked = True
                        break
                    inputs[field] = artifact
            if blocked:
                # Preserve a previously submitted job so it can be recovered later.
                if record["status"] in {"pending", "blocked", "completed"}:
                    record["status"] = "blocked"
                    persist()
                continue
            service_name = adapters.CAPABILITIES[job["uses"]]["service"]
            signature = intent_digest({"adapter": plan["adapter_version"], "uses": job["uses"],
                                       "service": plan["services"][service_name],
                                       "params": job["params"], "inputs": inputs})
            if record.get("signature", signature) != signature:
                raise ValueError("Resolved input signature changed; refusing to reuse or resubmit")
            modern_layout = plan.get("layout_version", 1) >= 2
            folder = (f".runner/{job['item']}/{job['step']}" if modern_layout
                      else f"items/{job['item']}/{job['step']}")
            manifest_path = directory / folder / "artifacts.json"
            # Recover publication interrupted between manifest and state commit.
            if manifest_path.is_file():
                try:
                    manifest = read_json(manifest_path)
                except (OSError, ValueError):
                    manifest = {}
                if not isinstance(manifest, dict):
                    manifest = {}
                if (manifest.get("signature") == signature and manifest.get("artifacts")
                        and all(state.valid(directory, a) for a in manifest["artifacts"].values())):
                    record.update(status="completed", signature=signature, artifacts=manifest["artifacts"])
                    record.pop("error", None)
                    persist()
                    continue
            action = recovery_action(record)
            if action == "uncertain":
                record.update(status="uncertain", error="Submission/result cannot be recovered automatically")
                persist()
            if action in {"uncertain", "stop"}:
                continue
            if plan.get("layout_version") == 3 and not record.get("job_id"):
                siblings = [j for j in plan["jobs"] if j["item"] == job["item"]
                            and adapters.CAPABILITIES[j["uses"]]["output"] == adapters.CAPABILITIES[job["uses"]]["output"]]
                stem = job["item"] if len(siblings) == 1 else f"{job['item']}-{job['step']}"
                existing = [directory / (stem + suffix) for suffix in (".png", ".jpg", ".jpeg", ".webp", ".mp4")
                            if (directory / (stem + suffix)).is_file()]
                if existing and job.get("reuse_existing") and len(existing) == 1 and record["status"] in {"pending", "blocked"}:
                    path = existing[0]
                    data = path.read_bytes()
                    if adapter_type := adapters.CAPABILITIES[job["uses"]]["output"]:
                        if adapter_type == "image":
                            suffix, mime = state.image_format(data)
                            if path.suffix.lower() != suffix:
                                raise ValueError("Existing image filename/content mismatch")
                        elif len(data) >= 32 and data[4:8] == b"ftyp" and path.suffix.lower() == ".mp4":
                            mime = "video/mp4"
                        else:
                            raise ValueError("Invalid existing video")
                        artifact = {"type": adapter_type, "path": path.relative_to(directory).as_posix(),
                                    "size": len(data), "sha256": sha256_file(path), "mime": mime}
                        state.save(manifest_path, {"signature": signature,
                                                  "origin": "explicit-existing-file",
                                                  "artifacts": {adapter_type: artifact}})
                        record.update(status="completed", origin="explicit-existing-file",
                                      signature=signature, artifacts={adapter_type: artifact})
                        persist()
                        continue
                if existing:
                    record.update(status="failed", error="Output filename is already occupied; refusing GPU submission")
                    persist()
                    continue
            bound = runtime[service_name]
            adapter = adapter_factory(job["uses"], bound["endpoint"], bound["headers"])
            try:
                if not record.get("job_id"):
                    payload = adapter.prepare(job["params"], inputs, directory)
                    state.save(directory / folder / "request.json", {
                        "uses": job["uses"], "params": job["params"], "inputs": inputs, "signature": signature})
                    record.update(status="submitting", signature=signature)
                    persist()  # Must happen before the only generation POST.
                    response = adapter.submit(payload, timeout)
                    if "job_id" in response:
                        record.update(status="running", job_id=response["job_id"])
                        persist()  # Must happen before polling.
                    else:
                        record["status"] = "collecting"
                        persist()
                        data = response["data"]
                if record.get("job_id"):
                    deadline = time.monotonic() + timeout
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("Polling deadline exceeded")
                        remote = adapter.poll(record["job_id"], min(180, remaining))
                        if remote in {"failed", "cancelled"}:
                            record.update(status="failed", error=f"Remote task {remote}")
                            persist()
                            break
                        if remote == "completed":
                            record["status"] = "collecting"
                            persist()
                            data = adapter.collect(record["job_id"], min(600, max(0.01, deadline - time.monotonic())))
                            break
                        if remote not in {"pending", "queued", "running", "processing", "in_progress"}:
                            raise ValueError("Unrecognized remote task status")
                        time.sleep(min(poll, max(0, deadline - time.monotonic())))
                    if record["status"] == "failed":
                        continue
                if modern_layout:
                    same_type = sum(
                        other["item"] == job["item"]
                        and adapters.CAPABILITIES[other["uses"]]["output"] == adapter.output
                        for other in plan["jobs"]
                    )
                    stem = job["item"] if same_type == 1 else f"{job['item']}-{job['step']}"
                    artifact = state.publish(directory, "", adapter.output, data, filename=stem,
                                             refuse_existing=plan.get("layout_version", 1) >= 3)
                else:
                    artifact = state.publish(directory, folder, adapter.output, data)
                manifest = {"signature": signature, "artifacts": {adapter.output: artifact}}
                state.save(manifest_path, manifest)
                record.update(status="completed", artifacts=manifest["artifacts"])
                record.pop("error", None)
                persist()
            except SubmissionRejected:
                record.update(status="pending", error="Gateway did not forward submission; resume later")
                persist()
            except Exception as exc:
                # Conservative for POST: transport and response errors may follow acceptance.
                # Do not persist exception text: upstream responses can contain credentials.
                record["status"] = "uncertain" if record.get("job_id") or record["status"] in {"submitting", "collecting"} else "failed"
                record["error"] = type(exc).__name__ + ": operation did not complete; resume queries known job IDs only"
                persist()
            print(f"{job['key']}: {record['status']}", flush=True)
        result = report(plan, records)
        state.save(directory / "report.json", result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "run"):
        p = sub.add_parser(command)
        p.add_argument("task")
        p.add_argument("--endpoints", default="endpoints.example.json")
        if command == "run":
            p.add_argument("--output", default="outputs")
            p.add_argument("--dry-run", action="store_true")
            p.add_argument("--timeout", type=float, default=3600)
            p.add_argument("--poll", type=float, default=2)
    for command in ("resume", "status"):
        p = sub.add_parser(command)
        p.add_argument("directory")
        if command == "resume":
            p.add_argument("--timeout", type=float, default=3600)
            p.add_argument("--poll", type=float, default=2)
    args = parser.parse_args()
    try:
        if args.command in {"plan", "run"}:
            plan = build_plan(args.task, endpoints_path=args.endpoints)
            if args.command == "plan" or args.dry_run:
                print(json.dumps({"name": plan["name"], "calls": len(plan["jobs"]),
                                  "services": list(plan["services"]), "jobs": plan["jobs"]}, ensure_ascii=False, indent=2))
                return
            directory = new_run(plan, output=args.output)
            print(f"Run: {directory}", flush=True)
        else:
            directory = state.local_path(ROOT, args.directory)
        if args.command == "status":
            result = report(read_json(directory / "plan.json"), read_json(directory / "state.json")["jobs"])
        else:
            result = execute(directory, timeout=args.timeout, poll=args.poll)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if any(status != "completed" and count for status, count in result["counts"].items()):
            raise SystemExit(1)
    except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
        parser.exit(2, f"{type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    main()
