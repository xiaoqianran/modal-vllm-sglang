"""Standalone OpenAI-compatible LLM HTTP service; no media task scheduler."""
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import modal

from runtime.config.model_spec import validate
from runtime.config.model_cache import verify_cache, command_argv
from runtime.serving.model_runtime import build_image
from runtime.serving.modal_setup import model_volume, model_mounts
from runtime.serving.process_lifecycle import wait_for_health, stop_process
from runtime.utils.files import read_json


def commit_engine_cache(volume, service):
    started = time.monotonic()
    try:
        volume.commit()
    except Exception as exc:
        print(json.dumps({
            "event": "llm_engine_cache_commit_failed",
            "service": service,
            "error": type(exc).__name__,
        }), flush=True)
    else:
        print(json.dumps({
            "event": "llm_engine_cache_committed",
            "service": service,
            "seconds": round(time.monotonic() - started, 3),
        }), flush=True)


def serve(self):
    """The engine gateway is started by lifecycle hooks."""
    pass


class Model:
    """Module-level class imported from source by the remote Python runtime."""

    def _load_spec(self):
        return validate(read_json(Path("/opt/model.json")))

    @modal.enter()
    def start(self):
        self.spec = self._load_spec()
        spec = self.spec
        self.volume = model_volume()
        api_key_secret = spec.get("api_key_secret")
        persistent_cache = spec.get("env", {}).get("VLLM_CACHE_ROOT", "").startswith("/data/")
        started = time.monotonic()
        paths, self.cache_fingerprint = verify_cache(spec, phase="engine_start")
        argv = command_argv(spec, paths)
        headers = None
        if api_key_secret:
            argv.extend(["--middleware", "runtime.serving.llm_auth.BearerAuth"])
            headers = {"Authorization": "Bearer " + os.environ["LLM_API_KEY"]}
        engine_started = time.monotonic()
        self.process = subprocess.Popen(argv)
        try:
            wait_for_health(
                self.process,
                f"http://127.0.0.1:{spec['port']}/health",
                timeout=spec["startup_timeout"] - 60,
                headers=headers,
            )
        except BaseException:
            stop_process(self.process)
            raise
        ready = time.monotonic()
        print(json.dumps({
            "event": "llm_ready",
            "service": spec["service"],
            "cache_fingerprint": self.cache_fingerprint,
            "weight_cache_check_seconds": round(engine_started - started, 3),
            "engine_to_health_seconds": round(ready - engine_started, 3),
            "startup_seconds": round(ready - started, 3),
        }), flush=True)
        if persistent_cache:
            threading.Thread(
                target=commit_engine_cache,
                args=(self.volume, spec["service"]),
                daemon=True,
            ).start()

    serve = modal.web_server(
        port=int(os.environ.get("MODEL_RUNTIME_WEB_PORT", "8000")),
        startup_timeout=float(os.environ.get("MODEL_RUNTIME_WEB_TIMEOUT", "3600")),
        requires_proxy_auth=os.environ.get("MODEL_RUNTIME_PROXY_AUTH", "1") == "1",
    )(serve)

    @modal.exit()
    def stop(self):
        process = getattr(self, "process", None)
        if process is not None:
            stop_process(process)


def create_llm_app(config_path):
    """Mount CPU-prepared model weights, start vLLM, and expose its HTTP API."""
    config_path = Path(config_path)
    spec = validate(read_json(config_path))
    if spec["kind"] != "llm":
        raise ValueError("Expected an LLM model configuration")

    volume_name = os.environ.get("MODEL_VOLUME_NAME", "model-store")
    app = modal.App(spec["app_name"])
    api_key_secret = spec.get("api_key_secret")
    runtime_env = {"MODEL_VOLUME_NAME": volume_name,
                   "MODEL_RUNTIME_WEB_PORT": str(spec["port"]),
                   "MODEL_RUNTIME_WEB_TIMEOUT": str(spec["startup_timeout"]),
                   "MODEL_RUNTIME_PROXY_AUTH": "0" if api_key_secret else "1"}
    image = build_image(spec, offline=True).add_local_file(
        str(config_path), "/opt/model.json", copy=False
    )
    secrets = (
        [modal.Secret.from_name(api_key_secret, required_keys=["LLM_API_KEY"])]
        if api_key_secret else []
    )
    original_serve = Model.__dict__["serve"]
    Model.serve = modal.web_server(
        port=spec["port"],
        label=spec["endpoint_label"],
        startup_timeout=spec["startup_timeout"],
        requires_proxy_auth=not api_key_secret,
    )(serve)
    try:
        model_handle = app.cls(
            image=image,
            env=runtime_env,
            serialized=False,
            gpu=spec["gpu"],
            cpu=spec["cpu"],
            memory=spec["memory"],
            volumes=model_mounts(spec),
            secrets=secrets,
            min_containers=0,
            max_containers=1,
            scaledown_window=spec["scaledown_window"],
            startup_timeout=spec["startup_timeout"],
            timeout=spec["timeout"],
        )(Model)
    finally:
        Model.serve = original_serve

    return app, model_handle
