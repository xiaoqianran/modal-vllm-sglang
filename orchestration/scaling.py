"""Read the actual Modal control plane; never warm a model or change scaling."""
from datetime import datetime, timezone


def inspect_scaling(spec, *, modal_api=None):
    if modal_api is None:
        import modal as modal_api

    app_name = spec.get("app_name", "modal-model-" + spec["service"])
    class_name = spec.get("modal_class", "Model")
    tag = class_name + ".*"
    app = modal_api.App.lookup(app_name).info()
    function_id = app.functions.get(tag)
    if not function_id:
        raise RuntimeError(f"Deployed class function not found: {app_name}/{tag}")

    # Class function names containing a dot are rejected by Function.from_name.
    # Resolve the actual service function ID from the deployment manifest instead.
    fn = modal_api.Function.from_id(function_id)
    settings = fn.autoscaler.settings()
    info = fn.info()
    stats = fn.get_current_stats()
    concurrency = info.concurrency_info
    expected = {
        "min_containers": 0,
        "max_containers": spec.get("max_containers", 1),
        "buffer_containers": spec.get("buffer_containers", 0),
        "scaledown_window": spec.get("scaledown_window", 30),
        "max_inputs": spec.get("max_inputs", 1),
        "target_inputs": spec.get("target_inputs", spec.get("max_inputs", 1)),
    }
    max_inputs = getattr(concurrency, "max_inputs", None)
    target_inputs = getattr(concurrency, "target_inputs", None)
    actual = {
        "min_containers": settings.min_containers or 0,
        "max_containers": settings.max_containers,
        "buffer_containers": settings.buffer_containers or 0,
        "scaledown_window": settings.scaledown_window,
        "max_inputs": max_inputs if max_inputs is not None else 1,
        "target_inputs": target_inputs if target_inputs is not None else (max_inputs or 1),
    }
    current = modal_api.App.lookup(app_name).info()
    if (current.lifecycle.version != app.lifecycle.version
            or current.functions.get(tag) != function_id):
        raise RuntimeError("Deployment changed during scaling inspection; retry")

    differences = {key: {"expected": value, "actual": actual[key]}
                   for key, value in expected.items() if actual[key] != value}
    capacity = {
        "backlog": stats.backlog,
        "runners": stats.num_total_runners,
        "running_inputs": stats.num_running_inputs,
        "input_headroom": stats.input_headroom,
    }
    # This is a sampled symptom, not proof of GPU scarcity or a quota issue.
    if differences:
        status = "configuration_mismatch"
    elif (capacity["backlog"] > 0 and capacity["input_headroom"] == 0
          and capacity["runners"] < expected["max_containers"]):
        status = "backlog_below_container_limit"
    elif capacity["runners"] >= expected["max_containers"]:
        status = "container_limit_reached"
    elif capacity["backlog"] == 0:
        status = "no_backlog"
    else:
        status = "capacity_available_or_starting"
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "service": spec["service"], "app": app_name, "app_revision": app.lifecycle.version,
        "function_id": function_id, "expected": expected, "actual": actual,
        "matches": not differences, "differences": differences,
        "capacity": capacity, "status": status,
    }
