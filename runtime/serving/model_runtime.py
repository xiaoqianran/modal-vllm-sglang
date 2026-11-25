"""Modal GPU lifecycle and image construction shared by all model configurations.

Remote classes are module-level and imported from source (serialized=False).
The model spec is read inside the container from /opt/model.json.
"""
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import modal

from runtime.tasks.task_identity import intent_digest
from runtime.utils.files import read_json
from runtime.config.model_spec import ROOT, validate
from runtime.config.model_cache import cache_directory, verify_cache, command_argv
from runtime.telemetry.recorder import Recorder
from runtime.serving.inference_execution import execute, publish_telemetry
from runtime.serving.deployment_probe import register_probes


def build_image(spec, *, offline):
    from runtime.serving.modal_setup import runtime_image
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
                         uv_packages=spec.get("uv_packages", ()),
                         python_version=spec.get("python_version"),
                         uv_torch_backend=spec.get("uv_torch_backend"),
                         uv_extra_index_url=spec.get("uv_extra_index_url"),
                         uv_only_binary=spec.get("uv_only_binary", False))


def serve(self):
    """The engine gateway is started by lifecycle hooks."""
    pass


class ModelRuntime:
    """Module-level, source-imported Modal class; no local spec closure."""

    def _load_spec(self):
        return validate(read_json(Path("/opt/model.json")))

    def _prepare_engine(self):
        from runtime.serving.process_lifecycle import stop_process, wait_for_health

        spec = self.spec
        startup = spec.get("startup_timeout", 3600)
        started = time.perf_counter()
        self.process = None
        self.gateway = None
        try:
            paths, self.cache_fingerprint = verify_cache(spec, phase="engine_start")
            self.process = subprocess.Popen(command_argv(spec, paths))
            wait_for_health(
                self.process,
                f"http://127.0.0.1:{spec.get('port', 8000)}/health",
                timeout=startup - 60,
            )
            if spec.get("memory_snapshot") == "gpu" and "snapshot_warmup" in spec:
                from runtime.serving.snapshot_warmup import warm_image_engine
                warm_image_engine(spec, timeout=max(1, startup - 60 - (time.perf_counter() - started)))
        except BaseException:
            if self.process is not None:
                stop_process(self.process)
            raise
        self.engine_initialization_seconds = time.perf_counter() - started
        print(json.dumps({
            "event": "engine_initialized",
            "service": spec["service"],
            "engine_initialization_seconds": self.engine_initialization_seconds,
            "memory_snapshot": spec.get("memory_snapshot", "off"),
        }), flush=True)

    @modal.enter(snap=False)
    def start(self):
        from runtime.serving.modal_setup import model_volume

        self.spec = self._load_spec()
        spec = self.spec
        gpu_snapshot = spec.get("memory_snapshot", "off") == "gpu"
        self.volume = model_volume()
        container_id = os.environ.get("MODAL_TASK_ID")
        from runtime.telemetry.recorder import cloud_recorder
        self.telemetry = (
            cloud_recorder(Path("/data"), container_id)
            if container_id else Recorder()
        )
        try:
            with self.telemetry.span(
                "startup",
                mode="post_snapshot_hook" if gpu_snapshot else "full_initialization",
                cpu=spec.get("cpu", 4),
                memory_mib=spec.get("memory", 32768),
                gpu=spec.get("gpu", "RTX-PRO-6000"),
                spec_hash=intent_digest(spec),
                model=spec["model"],
                image=spec["image"],
            ) as fields:
                self._start()
                fields.update(
                    engine_initialization_seconds=self.engine_initialization_seconds,
                    initialization_is_snapshot_history=gpu_snapshot,
                    cache_fingerprint=self.cache_fingerprint,
                )
        except BaseException:
            if hasattr(self, "resources"):
                self.resources.stop()
            try:
                if self.telemetry.writer is not None:
                    self.telemetry.writer.flush()
                    self.volume.commit()
            except Exception:
                pass
            raise

    def _start(self):
        from runtime.telemetry.sampler import ResourceMonitor
        from runtime.serving.process_lifecycle import (
            monitor_processes, stop_process, wait_for_health,
        )

        spec = self.spec
        gpu_snapshot = spec.get("memory_snapshot", "off") == "gpu"
        startup = spec.get("startup_timeout", 3600)
        started = time.perf_counter()
        self.batch_lock = threading.Lock()
        self.stopping = threading.Event()
        self.resources = ResourceMonitor(
            interval=spec.get("telemetry_interval", 5), recorder=self.telemetry
        )
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
                    raise RuntimeError(
                        "Model cache changed since engine initialization; "
                        "redeploy to rebuild snapshot"
                    )
            wait_for_health(
                self.process,
                f"http://127.0.0.1:{spec.get('port', 8000)}/health",
                timeout=startup - 60,
            )
            self.gateway = subprocess.Popen([
                "python", "-m", "runtime.serving.inference_gateway",
                "--config", "/opt/model.json",
            ])
            port = spec.get("proxy_port", spec.get("port", 8000) + 1)
            wait_for_health(self.gateway, f"http://127.0.0.1:{port}/health", timeout=120)
        except BaseException:
            if self.gateway is not None:
                stop_process(self.gateway)
            if self.process is not None:
                stop_process(self.process)
            raise

        self.monitor = monitor_processes(
            (self.process, self.gateway), self.volume,
            label=spec["service"], stopping=self.stopping,
            on_failure=lambda: os._exit(1),
        )
        self.startup_seconds = time.perf_counter() - started
        print(json.dumps({
            "event": "model_ready",
            "service": spec["service"],
            "container": os.environ.get("MODAL_TASK_ID"),
            "cache_fingerprint": self.cache_fingerprint,
            "startup_seconds": self.startup_seconds,
            "startup_measurement": "post_snapshot_hook" if gpu_snapshot else "full_initialization",
            "engine_initialization_seconds": self.engine_initialization_seconds,
            "memory_snapshot": spec.get("memory_snapshot", "off"),
            "snapshot_capture_id": getattr(self, "snapshot_capture_id", None),
            "resources": self.resources.snapshot(),
        }), flush=True)

    @modal.method()
    def deployment_info(self):
        """Explicit live check: starts the GPU container, but generates no media."""
        spec = self.spec
        volume_name = os.environ.get("MODEL_VOLUME_NAME", "model-store")
        gpu_snapshot = spec.get("memory_snapshot", "off") == "gpu"
        return {
            "service": spec["service"],
            "container_id": os.environ.get("MODAL_TASK_ID"),
            "spec_hash": intent_digest(spec),
            "deployment_version": os.environ.get("MODEL_DEPLOY_VERSION", "manual"),
            "volume": volume_name,
            "model_volume": volume_name,
            "cache_fingerprint": self.cache_fingerprint,
            "methods": ["generate_one", "deployment_info"],
            "image": spec["image"],
            "max_inputs": spec.get("max_inputs", 1),
            "sdk_inference_concurrency": 1,
            "cloud_artifacts": 1,
            "telemetry_schema_version": 1,
            "cpu": spec.get("cpu", 4),
            "memory": spec.get("memory", 32768),
            "startup_seconds": self.startup_seconds,
            "startup_measurement": "post_snapshot_hook" if gpu_snapshot else "full_initialization",
            "engine_initialization_seconds": self.engine_initialization_seconds,
            "memory_snapshot": spec.get("memory_snapshot", "off"),
            "snapshot_capture_id": getattr(self, "snapshot_capture_id", None),
            "resources": self.resources.snapshot(),
        }

    serve = modal.web_server(
        port=int(os.environ.get("MODEL_RUNTIME_WEB_PORT", "8001")),
        startup_timeout=float(os.environ.get("MODEL_RUNTIME_WEB_TIMEOUT", "3600")),
        requires_proxy_auth=True,
    )(serve)

    @modal.method()
    def generate_one(self, item, prefix, expected_cache_fingerprint, expected_spec_hash):
        spec = self.spec
        requested = time.perf_counter()
        if expected_spec_hash != intent_digest(spec):
            raise RuntimeError(
                "Local configuration differs from deployed model.json; deploy it first"
            )
        if expected_cache_fingerprint != self.cache_fingerprint:
            raise RuntimeError("Prepared model changed; restart with the current cache")
        context = item.get("_telemetry", {})
        telemetry = self.telemetry.bind(**{
            key: context[key] for key in ("task_id", "job_key") if key in context
        })
        with telemetry.span("wait", reason="lock"):
            self.batch_lock.acquire()
        try:
            started = time.perf_counter()
            print(json.dumps({
                "event": "generate_one",
                "id": item["id"],
                "prefix": prefix,
                "container": os.environ.get("MODAL_TASK_ID"),
                "input": modal.current_input_id(),
            }), flush=True)
            record = next(execute(
                spec, [item], prefix, self.volume, self.cache_fingerprint,
                recorder=telemetry, background_telemetry=True,
            ))
            print(json.dumps({
                "event": "generate_one_completed",
                "id": item["id"],
                "prefix": prefix,
                "service": spec["service"],
                "lock_wait_seconds": started - requested,
                "execution_seconds": time.perf_counter() - started,
                "resources": self.resources.snapshot(refresh=False),
                "container": os.environ.get("MODAL_TASK_ID"),
                "input": modal.current_input_id(),
            }), flush=True)
            return record
        except BaseException:
            metadata = Path("/data").joinpath(*prefix.split("/"), item["id"] + ".json")
            try:
                if metadata.is_file():
                    publish_telemetry(
                        telemetry, metadata, read_json(metadata), self.volume,
                        background=True,
                    )
            except Exception:
                pass
            raise
        finally:
            self.batch_lock.release()

    @modal.exit()
    def stop(self):
        from runtime.serving.process_lifecycle import stop_process

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


class SnapshotModelRuntime(ModelRuntime):
    """Snapshot-enabled variant; only this class declares a pre-snapshot hook."""

    @modal.enter(snap=True)
    def capture_engine(self):
        from uuid import uuid4
        self.spec = self._load_spec()
        self._prepare_engine()
        self.snapshot_capture_id = uuid4().hex
        print(json.dumps({"event": "snapshot_capture_ready",
                          "service": self.spec["service"],
                          "capture_id": self.snapshot_capture_id}), flush=True)


# The non-snapshot and snapshot variants retain the same deployed class tags.
class Model(ModelRuntime):
    pass


class Moss(ModelRuntime):
    pass


class Pika(ModelRuntime):
    pass


class H3(ModelRuntime):
    pass


class Qwen38Pi(ModelRuntime):
    pass


_RUNTIME_CLASSES = {cls.__name__: cls for cls in (Model, Moss, Pika, H3, Qwen38Pi)}


def create_app(config_path):
    """Register a module-level class; the remote Python imports source, not local bytecode."""
    from runtime.serving.modal_setup import model_mounts
    from runtime.config.storage import runtime_volume_name

    config_path = Path(config_path)
    spec = validate(read_json(config_path))
    volume_name = runtime_volume_name()
    app = modal.App(spec.get("app_name", "modal-model-" + spec["service"]))
    deployment_version = os.environ.get("MODEL_DEPLOY_VERSION", "manual")

    provision_image = (
        modal.Image.debian_slim(python_version="3.12")
        .env({
            "MODEL_DEPLOY_VERSION": deployment_version,
            "MODEL_VOLUME_NAME": volume_name,
        })
        .add_local_python_source("runtime")
    )
    register_probes(app, provision_image, deployment_version)

    startup = spec.get("startup_timeout", 3600)
    proxy_port = spec.get("proxy_port", spec.get("port", 8000) + 1)
    runtime_env = {"MODEL_VOLUME_NAME": volume_name,
                   "MODEL_DEPLOY_VERSION": deployment_version,
                   "MODEL_RUNTIME_WEB_PORT": str(proxy_port),
                   "MODEL_RUNTIME_WEB_TIMEOUT": str(startup)}
    image = build_image(spec, offline=True).add_local_file(
        str(config_path), "/opt/model.json", copy=False
    )
    if spec.get("validator_module"):
        relative = Path(*spec["validator_module"].split(".")).with_suffix(".py")
        validator_file = ROOT / relative
        if not validator_file.is_file():
            raise FileNotFoundError(f"Missing model validator: {validator_file}")
        image = image.add_local_file(
            str(validator_file), "/opt/" + relative.as_posix(), copy=False
        )

    gpu_snapshot = spec.get("memory_snapshot", "off") == "gpu"
    class_name = spec.get("modal_class", "Model")
    if gpu_snapshot:
        from runtime.serving.snapshot_classes import RUNTIME_CLASSES
    else:
        RUNTIME_CLASSES = _RUNTIME_CLASSES
    if class_name not in RUNTIME_CLASSES:
        raise ValueError(f"Unknown modal_class {class_name!r}; register a module-level runtime class")
    runtime_cls = RUNTIME_CLASSES[class_name]
    original_serve = serve
    runtime_cls.serve = modal.web_server(
        port=proxy_port,
        label=spec.get("endpoint_label", spec["service"] + "-serve"),
        startup_timeout=startup,
        requires_proxy_auth=True,
    )(original_serve)
    try:
        wrapped_cls = modal.concurrent(max_inputs=spec.get("max_inputs", 1))(runtime_cls)
        model_handle = app.cls(
            image=image,
            env=runtime_env,
            serialized=False,
            gpu=spec.get("gpu", "RTX-PRO-6000"),
            cpu=spec.get("cpu", 4),
            memory=spec.get("memory", 32768),
            volumes=model_mounts(spec),
            min_containers=0,
            max_containers=1,
            scaledown_window=spec.get("scaledown_window", 30),
            enable_memory_snapshot=gpu_snapshot,
            experimental_options={"enable_gpu_snapshot": True} if gpu_snapshot else {},
            startup_timeout=startup,
            timeout=spec.get("timeout", 14400),
        )(wrapped_cls)
    finally:
        # The registration captures Modal's web-server metadata. Restore the
        # source class so multiple model apps can be created in one process.
        del runtime_cls.serve

    return app, model_handle
