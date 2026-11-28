"""Read Modal capacity and optionally request a bounded batch override."""
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


# The conservative defaults are based on the 032/033 4K traces. These
# estimate *additional delay* before the second runner is useful, not
# guaranteed platform provisioning latency or a measured GPU bill.
SLOT_SECONDS = 12.5
STARTUP_SECONDS = 180.0
MIN_GAIN_SECONDS = 30.0
MAX_WINDOW_SECONDS = 300.0


def capacity_decision(remaining, *, runners=1, max_containers=2,
                      slot_seconds=SLOT_SECONDS, startup_seconds=STARTUP_SECONDS,
                      min_gain_seconds=MIN_GAIN_SECONDS):
    """Compare one useful runner against one additional, delayed runner."""
    import math
    if type(remaining) is not int or remaining < 0:
        raise ValueError("remaining must be a nonnegative integer")
    if type(runners) is not int or runners < 0 or type(max_containers) is not int or max_containers < 1:
        raise ValueError("Invalid runner counts")
    if (not all(math.isfinite(v) for v in (slot_seconds, startup_seconds, min_gain_seconds))
            or slot_seconds <= 0 or startup_seconds < 0 or min_gain_seconds < 0):
        raise ValueError("Invalid scaling estimates")
    work = remaining * slot_seconds
    # Requests are whole images: a runner cannot split a single diffusion job.
    first_slots = min(remaining, int(startup_seconds // slot_seconds))
    remaining_slots = remaining - first_slots
    if remaining_slots >= 2:
        # Search the possible assignment of the tail to two runners. The
        # first runner can already have a partially executed image at D.
        second_jobs = min(remaining, max(0, int((work - startup_seconds) / (2 * slot_seconds))))
        candidates = {second_jobs, min(remaining, second_jobs + 1)}
        two = min(max((remaining - n) * slot_seconds, startup_seconds + n * slot_seconds)
                  if n else work for n in candidates)
    else:
        two = work
    gain = max(0.0, work - two)
    if max_containers < 2 or runners >= 2:
        reason = "capacity_limit"
    elif remaining < 2 or startup_seconds >= work:
        reason = "startup_exceeds_work"
    elif gain <= 0 or gain < min_gain_seconds:
        reason = "insufficient_gain"
    else:
        reason = "request_second_runner"
    return {"remaining": remaining, "one_runner_seconds": work,
            "two_runner_seconds": two, "estimated_gain_seconds": gain,
            "additional_startup_seconds": startup_seconds,
            "request": reason == "request_second_runner", "reason": reason}


def _app_name(spec):
    return spec.get("app_name", "modal-model-" + spec["service"])


def _identity(report):
    return {key: report[key] for key in ("app_revision", "function_id")}


def _settings(report):
    return {key: report["actual"][key] for key in ("min_containers", "buffer_containers")}


def _restore_locked(spec, modal_api, registry, lease):
    """Restore only our exact override; never overwrite an unrelated change."""
    key = "capacity-override:" + _app_name(spec)
    marker = registry.get(key)
    if marker is None:
        return {"status": "no_override"}
    lease.assert_owned()
    report = inspect_scaling(spec, modal_api=modal_api)
    if _identity(report) != marker["identity"]:
        # Modal resets overrides on deployment. Do not mutate the new revision.
        registry.pop(key, None)
        return {"status": "deployment_changed", "previous": marker["identity"]}
    actual = _settings(report)
    unchanged = {k: v for k, v in report["actual"].items() if k not in marker["before"]}
    if unchanged != marker["other_settings"]:
        raise RuntimeError("Other autoscaler settings changed; retained override receipt for manual review")
    if actual == marker["after"]:
        fn = modal_api.Function.from_id(report["function_id"])
        lease.assert_owned()
        with lease.external("autoscaler.restore"):
            fn.autoscaler.update(**marker["before"])
        report = inspect_scaling(spec, modal_api=modal_api)
        if _identity(report) != marker["identity"]:
            registry.pop(key, None)
            return {"status": "deployment_changed"}
        actual = _settings(report)
    if actual != marker["before"]:
        raise RuntimeError("Unexpected autoscaler settings; retained override receipt for manual review")
    registry.pop(key, None)
    return {"status": "restored", "settings": actual}


def restore_capacity(spec, modal_api, registry):
    """Explicit recovery after a stopped controller; requires exclusive ownership."""
    from orchestration.leases import Lease
    app = _app_name(spec)
    if registry.get("capacity-override:" + app) is None:
        return {"status": "no_override"}
    with Lease(registry, "capacity:" + app) as lease:
        return _restore_locked(spec, modal_api, registry, lease)


def _remaining(directory, plan_hash, keys):
    from runtime.utils.files import read_json
    current = read_json(directory / "state.json")
    if current["plan_hash"] != plan_hash:
        return 0, 0  # A changed batch is not covered by this capacity request.
    jobs = [current["jobs"][key] for key in keys]
    pending = sum(r["status"] in {"pending", "blocked", "submitting"} and not r.get("call_id") for r in jobs)
    outstanding = sum(r["status"] in {"pending", "blocked", "submitting", "queued"} for r in jobs)
    return pending, outstanding


class BatchCapacity:
    """Opt-in, one local controller; no permanent cloud CPU service or dummy GPU inputs.

    Modal's autoscaler override has no TTL. The controller holds a shared
    lease, persists the prior settings before mutation, and restores in
    finally. Hard termination can leave the receipt and override behind;
    restore-scaling (or the next qualifying batch) reconciles it under the lease.
    """
    def __init__(self, modal_api, registry):
        from collections import OrderedDict
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event, Lock
        self.modal, self.registry = modal_api, registry
        self.stop, self.lock = Event(), Lock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="batch-capacity")
        self.seen = OrderedDict()
        self.active = {}

    def consider(self, directory, plan, service, ready, members):
        if service != "minimax-omni" or not ready.get("version") or not ready.get("app_revision"):
            return
        spec = plan["services"][service]["spec"]
        keys = tuple(key for group in members.values() for key in group)
        jobs = {job["key"]: job for job in plan["jobs"]}
        if not all(jobs[key]["uses"] == "minimax-omni.t2i"
                   and jobs[key]["params"].get("size") == "4096x2304"
                   and jobs[key]["params"].get("num_inference_steps") == 6
                   for key in keys):
            return  # No measured startup/throughput model for this workload.
        if not capacity_decision(len(keys), max_containers=spec.get("max_containers", 1))["request"]:
            return
        app = _app_name(spec)
        identity = (app, ready["app_revision"], plan["hash"])
        with self.lock:
            previous = self.active.get(app)
            if self.stop.is_set() or identity in self.seen or (previous and not previous.done()):
                return
            self.seen[identity] = True
            if len(self.seen) > 256:
                self.seen.popitem(last=False)
            self.active[app] = self.pool.submit(
                self._run, directory, plan["hash"], keys, spec, ready["app_revision"])

    def _run(self, directory, plan_hash, keys, spec, revision):
        import json
        import time
        from orchestration.leases import Lease
        app = _app_name(spec)
        marker_key = "capacity-override:" + app

        def log(**fields):
            print(json.dumps({"event": "batch_capacity", "app": app,
                              "plan_hash": plan_hash, **fields}), flush=True)

        try:
            with Lease(self.registry, "capacity:" + app) as lease:
                _restore_locked(spec, self.modal, self.registry, lease)
                report = inspect_scaling(spec, modal_api=self.modal)
                if not report["matches"] or report["app_revision"] != revision:
                    log(status="skip", reason="deployment_mismatch", report=report)
                    return
                pending, outstanding = _remaining(directory, plan_hash, keys)
                stats = report["capacity"]
                remaining = min(outstanding, pending + stats["backlog"] + stats["running_inputs"])
                decision = capacity_decision(remaining, runners=stats["runners"],
                                             max_containers=spec.get("max_containers", 1))
                log(status="decision", **decision)
                if not decision["request"] or self.stop.is_set():
                    return
                fn = self.modal.Function.from_id(report["function_id"])
                before = _settings(report)
                after = {"min_containers": 2, "buffer_containers": 0}
                window = min(MAX_WINDOW_SECONDS, max(0.0,
                    decision["one_runner_seconds"] - 2 * MIN_GAIN_SECONDS))
                started = time.monotonic()
                # Re-check the deployed revision and all settings before recording
                # an intent that could authorize recovery after an ambiguous reply.
                latest = inspect_scaling(spec, modal_api=self.modal)
                if (_identity(latest) != _identity(report)
                        or latest["actual"] != report["actual"]):
                    log(status="skip", reason="deployment_changed_before_request")
                    return
                marker = {"identity": _identity(report), "before": before,
                          "after": after, "expires_at": time.time() + window,
                          "other_settings": {k: v for k, v in report["actual"].items()
                                             if k not in before}}
                lease.assert_owned()
                self.registry[marker_key] = marker
                reason = "controller_exit"
                try:
                    lease.assert_owned()
                    with lease.external("autoscaler.request"):
                        fn.autoscaler.update(**after)
                    log(status="requested", settings=after, window_seconds=window)
                    while not self.stop.wait(5):
                        lease.assert_owned()
                        elapsed = time.monotonic() - started
                        live = fn.get_current_stats()
                        pending, outstanding = _remaining(directory, plan_hash, keys)
                        remaining = min(outstanding, pending + live.backlog + live.num_running_inputs)
                        # More active inputs than one container can accept is
                        # positive evidence of parallel assignment, not merely
                        # an allocated/initializing runner.
                        if (live.num_total_runners >= 2
                                and live.num_running_inputs > spec.get("max_inputs", 1)):
                            reason = "parallel_inputs"
                            break
                        if elapsed >= window:
                            reason = "window_expired"
                            break
                        if outstanding == 0 or (elapsed >= 10 and remaining == 0):
                            reason = "no_work_remaining"
                            break
                        if elapsed >= 10 and not capacity_decision(
                                remaining, startup_seconds=max(0.0, STARTUP_SECONDS - elapsed),
                                max_containers=spec.get("max_containers", 1))["request"]:
                            reason = "remaining_gain_too_small"
                            break
                finally:
                    # Also reconcile a lost update reply. Leave the receipt
                    # intact if cleanup fails; do not hide an uncertain override.
                    restored = _restore_locked(spec, self.modal, self.registry, lease)
                    log(status="released", reason=reason, restore=restored)
        except Exception as exc:
            # Capacity is an optimization: never block or resubmit generation.
            log(status="error", error=f"{type(exc).__name__}: {exc}")

    def close(self):
        with self.lock:
            self.stop.set()
        self.pool.shutdown(wait=True, cancel_futures=True)
