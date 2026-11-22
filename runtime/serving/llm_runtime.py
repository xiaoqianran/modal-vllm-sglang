"""Standalone OpenAI-compatible LLM HTTP service; no media task scheduler."""
import json
import os
import subprocess
import threading
import time
from pathlib import Path

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
        print(json.dumps({"event": "llm_engine_cache_commit_failed", "service": service,
                          "error": type(exc).__name__}), flush=True)
    else:
        print(json.dumps({"event": "llm_engine_cache_committed", "service": service,
                          "seconds": round(time.monotonic() - started, 3)}), flush=True)


def create_llm_app(config_path):
    """Mount CPU-prepared model weights, start vLLM, and expose its HTTP API."""
    import modal

    config_path = Path(config_path)
    spec = validate(read_json(config_path))
    if spec["kind"] != "llm":
        raise ValueError("Expected an LLM model configuration")

    volume = model_volume()
    app = modal.App(spec["app_name"])
    api_key_secret = spec.get("api_key_secret")
    persistent_cache = spec.get("env", {}).get("VLLM_CACHE_ROOT", "").startswith("/data/")
    image = build_image(spec, offline=True).add_local_file(
        str(config_path), "/opt/model.json", copy=False
    )

    @app.cls(
        image=image,
        serialized=True,
        gpu=spec["gpu"],
        cpu=spec["cpu"],
        memory=spec["memory"],
        volumes=model_mounts(spec),
        secrets=[modal.Secret.from_name(api_key_secret, required_keys=["LLM_API_KEY"])] if api_key_secret else [],
        min_containers=0,
        max_containers=1,
        scaledown_window=spec["scaledown_window"],
        startup_timeout=spec["startup_timeout"],
        timeout=spec["timeout"],
    )
    class Model:
        @modal.enter()
        def start(self):
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
            print(json.dumps({"event": "llm_ready", "service": spec["service"],
                              "cache_fingerprint": self.cache_fingerprint,
                              "weight_cache_check_seconds": round(engine_started - started, 3),
                              "engine_to_health_seconds": round(ready - engine_started, 3),
                              "startup_seconds": round(ready - started, 3)}), flush=True)
            if persistent_cache:
                threading.Thread(target=commit_engine_cache, args=(volume, spec["service"]),
                                 daemon=True).start()

        @modal.web_server(
            port=spec["port"],
            label=spec["endpoint_label"],
            startup_timeout=spec["startup_timeout"],
            requires_proxy_auth=not api_key_secret,
        )
        def serve(self):
            pass

        @modal.exit()
        def stop(self):
            process = getattr(self, "process", None)
            if process is not None:
                stop_process(process)

    return app, Model
