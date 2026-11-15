"""Shared plan bindings and derived progress; no scheduler dependency."""
from collections import Counter

from shared.tasks.batch import intent_digest

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
    if plan.get("transport") == "sdk":
        result["download_errors"] = sum(bool(r.get("download_error")) for r in records.values())
        result["local_downloaded"] = sum(bool(r.get("local_artifact")) for r in records.values())
    return result
