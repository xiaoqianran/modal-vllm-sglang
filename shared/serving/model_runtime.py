"""Modal GPU lifecycle and image construction shared by all model configurations."""
import json
import subprocess
import threading
import os
import time
from pathlib import Path

from shared.tasks.batch import intent_digest
from shared.utils.files import read_json
from shared.config.model_spec import validate
from shared.config.model_cache import cache_directory, verify_cache, command_argv
from shared.telemetry.recorder import Recorder


from shared.serving.inference_execution import execute, publish_telemetry

def class_name(name):
    def decorate(cls):
        cls.__name__ = name
        return cls
    return decorate


def build_image(spec, *, offline):
    from shared.serving.modal_setup import runtime_image
    root = cache_directory(spec)
    env = {**spec.get("env", {}), "HF_HOME": root,
           "SGLANG_DIFFUSION_CACHE_ROOT": root + "/sglang"}
    if os.environ.get("MODEL_DEPLOY_VERSION"):
        env["MODEL_DEPLOY_VERSION"] = os.environ["MODEL_DEPLOY_VERSION"]
    if spec.get("memory_snapshot", "off") == "gpu":
        env.setdefault("TORCHINDUCTOR_COMPILE_THREADS", "1")
    if offline:
        env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    else:
        env.update(HF_HUB_OFFLINE="0", TRANSFORMERS_OFFLINE="0")
    return runtime_image(spec["image"], env=env, commands=spec.get("build_commands", ()),
                         uv_packages=spec.get("uv_packages", ()))


def create_app(config_path):
    """One GPU lifecycle shared by all configured models."""
    import modal
    from shared.serving.modal_setup import model_volume
    from shared.serving.runtime import monitor_processes, stop_process, wait_for_health
    config_path = Path(config_path)
    spec = validate(read_json(config_path))
    volume = model_volume()
    app = modal.App(spec.get("app_name", "modal-model-" + spec["service"]))
    deployment_version = os.environ.get("MODEL_DEPLOY_VERSION", "manual")

    @app.function(name="provision_info", image=modal.Image.debian_slim().env({"MODEL_DEPLOY_VERSION": deployment_version}), serialized=True)
    def provision_info():
        return deployment_version

    image = build_image(spec, offline=True).add_local_file(str(config_path), "/opt/model.json", copy=False)
    startup = spec.get("startup_timeout", 3600)
    gpu_snapshot = spec.get("memory_snapshot", "off") == "gpu"

    @app.cls(image=image, serialized=True, gpu=spec.get("gpu", "RTX-PRO-6000"),
             cpu=spec.get("cpu", 4), memory=spec.get("memory", 32768),
             volumes={"/mnt/modal": volume}, min_containers=0, max_containers=1,
             scaledown_window=spec.get("scaledown_window", 30),
             enable_memory_snapshot=gpu_snapshot,
             experimental_options={"enable_gpu_snapshot": True} if gpu_snapshot else {},
             startup_timeout=startup, timeout=spec.get("timeout", 14400))
    @modal.concurrent(max_inputs=spec.get("max_inputs", 1))
    @class_name(spec.get("modal_class", "Model"))
    class Model:
        def _prepare_engine(self):
            started = time.perf_counter()
            self.process = None
            self.gateway = None
            try:
                paths, self.cache_fingerprint = verify_cache(spec, phase="engine_start")
                self.process = subprocess.Popen(command_argv(spec, paths))
                wait_for_health(self.process, f"http://127.0.0.1:{spec.get('port', 8000)}/health", timeout=startup - 60)
            except BaseException:
                if self.process is not None:
                    stop_process(self.process)
                raise
            self.engine_initialization_seconds = time.perf_counter() - started
            print(json.dumps({"event": "engine_initialized", "service": spec["service"],
                              "engine_initialization_seconds": self.engine_initialization_seconds,
                              "memory_snapshot": spec.get("memory_snapshot", "off")}), flush=True)

        @modal.enter(snap=gpu_snapshot)
        def capture_engine(self):
            # Only engine state is captured. Runtime threads, identity and gateway
            # are created after restore; disabled snapshots use start() instead.
            if gpu_snapshot:
                self._prepare_engine()

        @modal.enter(snap=False)
        def start(self):
            from shared.telemetry.recorder import cloud_recorder
            container_id = os.environ.get("MODAL_TASK_ID")
            self.telemetry = cloud_recorder(Path("/mnt/modal"), container_id) if container_id else Recorder()
            try:
                with self.telemetry.span("startup", mode="post_snapshot_hook" if gpu_snapshot else "full_initialization",
                                         cpu=spec.get("cpu", 4), memory_mib=spec.get("memory", 32768),
                                         gpu=spec.get("gpu", "RTX-PRO-6000"), spec_hash=intent_digest(spec),
                                         model=spec["model"], image=spec["image"]) as fields:
                    self._start()
                    fields.update(engine_initialization_seconds=self.engine_initialization_seconds,
                                  initialization_is_snapshot_history=gpu_snapshot,
                                  cache_fingerprint=self.cache_fingerprint)
            except BaseException:
                if hasattr(self, "resources"):
                    self.resources.stop()
                try:
                    if self.telemetry.writer is not None:
                        self.telemetry.writer.flush()
                        volume.commit()
                except Exception:
                    pass
                raise

        def _start(self):
            from shared.telemetry.sampler import ResourceMonitor
            started = time.perf_counter()
            self.batch_lock = threading.Lock()
            self.stopping = threading.Event()
            self.resources = ResourceMonitor(interval=spec.get("telemetry_interval", 5), recorder=self.telemetry)
            self.telemetry.sampler = self.resources.request
            self.resources.start()
            self.gateway = None
            if not gpu_snapshot:
                self._prepare_engine()
            try:
                # Volume mutations do not invalidate Modal snapshots. Reject stale
                # captured weights rather than serving them under a new manifest.
                if gpu_snapshot:
                    _, fingerprint = verify_cache(spec, phase="post_snapshot")
                    if fingerprint != self.cache_fingerprint:
                        raise RuntimeError("Model cache changed since engine initialization; redeploy to rebuild snapshot")
                wait_for_health(self.process, f"http://127.0.0.1:{spec.get('port', 8000)}/health", timeout=startup - 60)
                self.gateway = subprocess.Popen(["python", "-m", "shared.serving.inference_gateway", "--config", "/opt/model.json"])
                port = spec.get("proxy_port", spec.get("port", 8000) + 1)
                wait_for_health(self.gateway, f"http://127.0.0.1:{port}/health", timeout=120)
            except BaseException:
                if self.gateway is not None:
                    stop_process(self.gateway)
                if self.process is not None:
                    stop_process(self.process)
                raise

            self.monitor = monitor_processes((self.process, self.gateway), volume, label=spec["service"],
                              stopping=self.stopping, on_failure=lambda: os._exit(1))
            self.startup_seconds = time.perf_counter() - started
            print(json.dumps({"event": "model_ready", "service": spec["service"],
                              "container": os.environ.get("MODAL_TASK_ID"),
                              "cache_fingerprint": self.cache_fingerprint,
                              "startup_seconds": self.startup_seconds,
                              "startup_measurement": "post_snapshot_hook" if gpu_snapshot else "full_initialization",
                              "engine_initialization_seconds": self.engine_initialization_seconds,
                              "memory_snapshot": spec.get("memory_snapshot", "off"),
                              "resources": self.resources.snapshot()}), flush=True)

        @modal.method()
        def deployment_info(self):
            """Explicit live check: starts the GPU container, but generates no media."""
            from shared.config.storage import VOLUME_NAME
            return {"service": spec["service"], "spec_hash": intent_digest(spec), "volume": VOLUME_NAME,
                    "cache_fingerprint": self.cache_fingerprint,
                    "methods": ["generate_one", "deployment_info"],
                    "image": spec["image"], "max_inputs": spec.get("max_inputs", 1),
                    "sdk_inference_concurrency": 1,
                    "cloud_artifacts": 1,
                    "telemetry_schema_version": 1,
                    "cpu": spec.get("cpu", 4), "memory": spec.get("memory", 32768),
                    "startup_seconds": self.startup_seconds,
                    "startup_measurement": "post_snapshot_hook" if gpu_snapshot else "full_initialization",
                    "engine_initialization_seconds": self.engine_initialization_seconds,
                    "memory_snapshot": spec.get("memory_snapshot", "off"),
                    "resources": self.resources.snapshot()}

        @modal.web_server(port=spec.get("proxy_port", spec.get("port", 8000) + 1),
                          label=spec.get("endpoint_label", spec["service"] + "-serve"), startup_timeout=startup, requires_proxy_auth=True)
        def serve(self):
            pass

        @modal.method()
        def generate_one(self, item, prefix, expected_cache_fingerprint, expected_spec_hash):
            requested = time.perf_counter()
            if expected_spec_hash != intent_digest(spec):
                raise RuntimeError("Local configuration differs from deployed model.json; deploy it first")
            if expected_cache_fingerprint != self.cache_fingerprint:
                raise RuntimeError("Prepared model changed; restart with the current cache")
            context = item.get("_telemetry", {})
            telemetry = self.telemetry.bind(**{key: context[key] for key in ("task_id", "job_key") if key in context})
            with telemetry.span("wait", reason="lock"):
                self.batch_lock.acquire()
            try:
                started = time.perf_counter()
                print(json.dumps({"event": "generate_one", "id": item["id"], "prefix": prefix,
                                  "container": os.environ.get("MODAL_TASK_ID"),
                                  "input": modal.current_input_id()}), flush=True)
                record = next(execute(spec, [item], prefix, volume, self.cache_fingerprint,
                                      recorder=telemetry, background_telemetry=True))
                print(json.dumps({"event": "generate_one_completed", "id": item["id"], "prefix": prefix,
                                  "service": spec["service"],
                                  "lock_wait_seconds": started - requested,
                                  "execution_seconds": time.perf_counter() - started,
                                  "resources": self.resources.snapshot(refresh=False),
                                  "container": os.environ.get("MODAL_TASK_ID"),
                                  "input": modal.current_input_id()}), flush=True)
                return record
            except BaseException:
                metadata = Path("/mnt/modal").joinpath(*prefix.split("/"), item["id"] + ".json")
                try:
                    if metadata.is_file():
                        publish_telemetry(telemetry, metadata, read_json(metadata), volume, background=True)
                except Exception:
                    pass
                raise
            finally:
                self.batch_lock.release()


        @modal.exit()
        def stop(self):
            # Initialization/capture can fail before runtime objects exist.
            if hasattr(self, "resources") and self.resources.thread.is_alive():
                self.resources.stop()
            if hasattr(self, "telemetry") and self.telemetry.writer is not None:
                self.telemetry.writer.close()
            if hasattr(self, "stopping"):
                self.stopping.set()
            if hasattr(self, "monitor"):
                self.monitor.join(timeout=60)
            else:
                for process in (getattr(self, "gateway", None), getattr(self, "process", None)):
                    if process is not None:
                        stop_process(process)

    return app, Model
