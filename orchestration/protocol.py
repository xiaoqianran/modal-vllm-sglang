"""Shared plan bindings and derived progress; no scheduler dependency."""
from collections import Counter

from runtime.tasks.task_identity import intent_digest

APP_NAME = "modal-vllm-sglang-orchestration"
PROVISION_REGISTRY = APP_NAME + "-provisioning"
SUBMISSION_REGISTRY = APP_NAME + "-generation-claims"
VERSION = 1


def run_id(plan, item):
    return intent_digest([plan["project"], plan["scene"], item])


def state_path(identity):
    if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
        raise ValueError("Invalid cloud workflow identity")
    return f"workflow-runs/{identity}/state.json"


def reference(binding):
    return binding["ref"].split(".", 1)

def bindings(plan):
    if plan.get("transport") != "sdk":
        raise ValueError("Only SDK workflow plans are supported")
    return {name: {"endpoint": "sdk://" + service["spec"].get("app_name", "modal-model-" + name), "headers": {}}
            for name, service in plan["services"].items()}


def report(plan, records):
    outputs = {}
    for job in plan["jobs"]:
        for name, binding in plan["outputs"].items():
            step, output = reference(binding)
            record = records[job["key"]]
            if step == job["step"] and record["status"] == "completed":
                outputs.setdefault(job["item"], {})[name] = record["artifacts"][output]
    result = {"name": plan["name"], "counts": dict(Counter(r["status"] for r in records.values())),
              "outputs": outputs}
    waiting = Counter(r.get("wait_detail") or r.get("wait_reason") for r in records.values()
                      if r["status"] in {"pending", "blocked"} and (r.get("wait_detail") or r.get("wait_reason")))
    if waiting:
        result["waiting_reasons"] = dict(waiting)
    if plan.get("transport") == "sdk":
        result["download_errors"] = sum(bool(r.get("download_error")) for r in records.values())
        result["local_downloaded"] = sum(bool(r.get("local_artifact")) for r in records.values())
        result["cloud_archived"] = sum(bool(r.get("cloud_archive")) for r in records.values())
        result["archive_errors"] = sum(bool(r.get("archive_error")) for r in records.values())
        from runtime.delivery import destinations
        result["delivery_pending"] = sum(
            record["status"] == "completed" and not all(
                record.get("local_artifact" if dest == "local" else "cloud_archive")
                for dest in destinations(plan)) for record in records.values())
    return result
