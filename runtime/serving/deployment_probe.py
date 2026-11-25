"""CPU-only version probes isolated from rolling deployment traffic."""
import re

PROBE_OPTIONS = {
    "cpu": 0.25, "memory": 512, "timeout": 15, "startup_timeout": 30,
    "single_use_containers": True, "scaledown_window": 2, "retries": 0,
}


def versioned_name(name, version):
    """A new source/cache identity must never call the old function tag."""
    if isinstance(version, str) and re.fullmatch(r"[0-9a-f]{64}", version):
        return name + "_" + version[:32]
    return name


def probe_name(version):
    return versioned_name("provision_info", version)


def assert_deployment_version(expected):
    """Reject stale runtime execution before any inference or workflow claim."""
    import os
    if expected is not None and os.environ.get("MODEL_DEPLOY_VERSION", "manual") != expected:
        raise RuntimeError("Deployment source version mismatch; refusing execution")


def register_probes(app, image, version):
    from runtime.serving.remote_workers import provision_info_entry

    # Keep the stable diagnostic endpoint for manual/legacy callers only.
    for name in dict.fromkeys(("provision_info", probe_name(version))):
        app.function(name=name, image=image, serialized=False,
                     env={"MODEL_DEPLOY_VERSION": version},
                     **PROBE_OPTIONS)(provision_info_entry)
