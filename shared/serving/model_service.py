"""Standard deployed image/video service, CPU cache preparation and SDK CLI.

Engine-specific behavior belongs in model.json's argv and image. This module
targets /v1/images/generations and /v1/videos, including inline keyframes.
"""
import json
import hashlib
import subprocess
import threading
import os
import time
from pathlib import Path

from shared.tasks.batch import completed_output, intent_digest
from shared.utils.files import atomic_json, read_json, sha256_file
from shared.utils.media import media_suffix
from shared.config.model_spec import validate
from shared.config.model_cache import cache_directory, verify_cache, prepare_cache, command_argv
from shared.utils.outputs import component
from shared.tasks.task_lifecycle import recovery_action, TERMINAL, SubmissionRejected
from shared.telemetry.recorder import Recorder

# Re-export task helpers for existing callers. New code imports task_spec directly.
from shared.tasks.task_spec import ROOT, load_task, prepare, result_prefix, execution_signature


def class_name(name):
    def decorate(cls):
        cls.__name__ = name
        return cls
    return decorate


def execute(spec, items, prefix, volume, cache_fingerprint="unverified", recorder=None, background_telemetry=False):
    from shared.serving.inference_api import VLLMImageClient, SGLangVideoClient
    parts = prefix.split("/")
    for part in parts:
        component(part)
    folder = Path("/mnt/modal").joinpath(*parts)
    prepared = [(item, *prepare(spec, item)) for item in items]
    if not prepared or len({x[0]["id"].lower() for x in prepared}) != len(prepared):
        raise ValueError("Nonempty batch with unique IDs required")
    port = spec.get("proxy_port", spec.get("port", 8000) + 1)
    client = (VLLMImageClient if spec["kind"] == "image" else SGLangVideoClient)(f"http://127.0.0.1:{port}")
    for item, payload, signature in prepared:
        context = item.get("_telemetry", {})
        telemetry = (recorder or Recorder()).bind(**{key: context[key] for key in ("task_id", "job_key") if key in context})
        started = time.perf_counter()
        metadata = folder / (item["id"] + ".json")
        old = read_json(metadata) if metadata.exists() else {}
        if old.get("intent_hash") not in (None, signature):
            raise ValueError("Remote task signature changed")
        execution_hash = execution_signature(signature, cache_fingerprint)
        if old and old.get("execution_hash") != execution_hash:
            raise ValueError("Remote model cache changed")
        if old.get("status") == "completed":
            if old.get("file") not in {item["id"] + s for s in (".png", ".jpg", ".webp", ".mp4")}:
                raise ValueError("Unsafe remote result filename")
            target = folder / old["file"]
            if not completed_output(target, metadata, signature):
                raise ValueError("Remote result damaged")
            yield old
            continue
        if old.get("status") == "collecting" and old.get("file") in {item["id"] + s for s in (".png", ".jpg", ".webp", ".mp4")}:
            target = folder / old["file"]
            if target.is_file() and sha256_file(target) == old.get("sha256"):
                old["status"] = "completed"
                atomic_json(metadata, old)
                volume.commit()
                yield old
                continue
        if not old and any((folder / (item["id"] + suffix)).exists() for suffix in (".png", ".jpg", ".webp", ".mp4")):
            raise FileExistsError("Unverified remote media already exists")
        action = recovery_action(old)
        if action == "stop":
            raise RuntimeError("Remote task failed; create a new task to generate again")
        if action == "uncertain":
            raise RuntimeError("Uncertain submission; inspect remote metadata before creating a new task")
        if action == "resume":
            job = old["job_id"]
        else:
            from shared.tasks.artifacts import resolve_frames
            if any(key.endswith("_artifact") for key in payload):
                with telemetry.span("transfer", operation="read_frames"):
                    payload = resolve_frames(payload, volume)
            # Persist before submit: a lost response never causes automatic resubmission.
            atomic_json(metadata, {"status": "submitting", "intent_hash": signature, "execution_hash": execution_hash})
            volume.commit()
            try:
                if spec["kind"] == "image":
                    with telemetry.span("generate", operation="image"):
                        data, _ = client.generate(payload, timeout=spec.get("timeout", 3600))
                else:
                    with telemetry.span("generate", operation="video_submit") as fields:
                        job = client.submit(payload, timeout=spec.get("timeout", 3600))
                        fields["engine_job_id"] = job
                    atomic_json(metadata, {"status": "running", "intent_hash": signature, "execution_hash": execution_hash, "job_id": job})
                    volume.commit()
            except SubmissionRejected:
                atomic_json(metadata, {"status": "pending", "intent_hash": signature, "execution_hash": execution_hash})
                volume.commit()
                raise
        if spec["kind"] == "video":
            telemetry = telemetry.bind(engine_job_id=str(job))
            def remember_failure(remote):
                if remote.get("status") in TERMINAL - {"completed"}:
                    record = read_json(metadata)
                    record["status"] = "failed"
                    atomic_json(metadata, record)
                    volume.commit()
            with telemetry.span("generate", operation="video_wait"):
                client.wait(job, timeout=spec.get("timeout", 3600), on_status=remember_failure)
            with telemetry.span("generate", operation="video_fetch"):
                data = client.content(job)
        suffix = media_suffix(data, spec["kind"])
        target = folder / (item["id"] + suffix)
        digest = hashlib.sha256(data).hexdigest()
        if target.exists() and sha256_file(target) != digest:
            raise FileExistsError(f"Refusing overwrite: {target}")
        record = {"id": item["id"], "file": target.name, "status": "collecting", "bytes": len(data),
                  "execution_seconds": time.perf_counter() - started,
                  "intent_hash": signature, "execution_hash": execution_hash, "sha256": digest}
        if spec["kind"] == "video":
            record["job_id"] = job
        if telemetry.writer is not None:
            record["telemetry"] = telemetry.writer.reference()
        with telemetry.span("save", bytes=len(data)):
            atomic_json(metadata, record)
            volume.commit()
            from shared.utils.files import atomic_write
            if not target.exists():
                atomic_write(target, data)
            record["status"] = "completed"
            atomic_json(metadata, record)
            volume.commit()
        publish_telemetry(telemetry, metadata, record, volume, background=background_telemetry)
        yield record


def publish_telemetry(recorder, metadata, record, volume, *, background=False):
    """Optional metadata publication cannot turn successful generation into failure."""
    if recorder.writer is None:
        return
    try:
        reference = record.get("telemetry") or recorder.writer.reference()
        if background:
            # Only the immutable manifest is written in the background. Never race
            # another job by rewriting generation metadata after releasing its lock.
            if not record.get("telemetry"):
                record["telemetry"] = reference
                atomic_json(metadata, record)
            recorder.writer.publish_async(reference, volume.commit)
            return
        record["telemetry"] = recorder.writer.publish(reference)
        atomic_json(metadata, record)
        volume.commit()
    except Exception as exc:
        record["telemetry_error"] = type(exc).__name__


def build_image(spec, *, offline):
    import os
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
    return runtime_image(spec["image"], env=env, commands=spec.get("build_commands", ()))


def create_app(config_path):
    """One GPU lifecycle shared by all configured models."""
    import modal
    from shared.serving.modal_setup import model_volume
    from shared.serving.runtime import monitor_processes, stop_process, wait_for_health
    config_path = Path(config_path)
    spec = validate(read_json(config_path))
    volume = model_volume()
    app = modal.App(spec.get("app_name", "modal-model-" + spec["service"]))
    import os
    deployment_version = os.environ.get("MODEL_DEPLOY_VERSION", "manual")

    @app.function(name="provision_info", image=modal.Image.debian_slim(), serialized=True)
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
                    "methods": ["generate_one", "generate_batch_stream", "deployment_info"],
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

        @modal.method()
        def generate_batch_stream(self, items, prefix, expected_cache_fingerprint, expected_spec_hash):
            if expected_spec_hash != intent_digest(spec):
                raise RuntimeError("Local configuration differs from deployed model.json; deploy it first")
            if expected_cache_fingerprint != self.cache_fingerprint:
                raise RuntimeError("Prepared model changed; restart with the current cache")
            with self.batch_lock:
                for record in execute(spec, items, prefix, volume, self.cache_fingerprint):
                    print(json.dumps({"event": "batch_item_completed", "service": spec["service"],
                                      "id": record["id"], "prefix": prefix,
                                      "execution_seconds": record.get("execution_seconds"),
                                      "resources": self.resources.snapshot()}), flush=True)
                    yield record

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


def optional_hf_download_secrets():
    """Use a configured HF_TOKEN, otherwise keep public downloads anonymous."""
    import modal
    try:
        secret = modal.Secret.from_name("huggingface-secret", required_keys=["HF_TOKEN"])
        secret.hydrate()
    except modal.exception.NotFoundError:
        print("HF download: huggingface-secret not found; using anonymous access", flush=True)
        return []
    print("HF download: huggingface-secret attached (token not displayed)", flush=True)
    return [secret]


def create_download_app(config_path):
    import modal
    from shared.serving.modal_setup import model_volume
    spec = validate(read_json(config_path))
    volume = model_volume()
    app = modal.App("prepare-" + spec["service"])
    if spec.get("cache", {}).get("mode") == "sglang_overlay":
        image = build_image(spec, offline=False)
    else:
        image = modal.Image.debian_slim(python_version="3.12").pip_install("huggingface_hub>=0.30,<2", "hf_xet")
        image = image.env({"PYTHONPATH": "/opt"}).add_local_dir(str(Path(__file__).parent.parent), "/opt/shared", copy=False)

    @app.function(name="download", image=image, serialized=True, cpu=4, memory=8192,
                  timeout=86400, volumes={"/mnt/modal": volume},
                  secrets=optional_hf_download_secrets())
    def download(refresh: bool = False, migrate_only: bool = False, repair: bool = False):
        if repair and not refresh and not migrate_only:
            try:
                return prepare_cache(spec, volume)
            except RuntimeError:
                return prepare_cache(spec, volume, refresh=True)
        return prepare_cache(spec, volume, refresh=refresh, migrate_only=migrate_only)

    @app.function(name="check_cache", image=image, serialized=True, cpu=2, memory=8192,
                  timeout=600, volumes={"/mnt/modal": volume})
    def check_cache():
        _, fingerprint = verify_cache(spec)
        return {"ready": True, "fingerprint": fingerprint}

    return app, download, check_cache


def run_download_cli(download, check_cache, *, check_only=False, refresh=False, migrate_only=False):
    if sum((check_only, refresh, migrate_only)) > 1:
        raise ValueError("Choose only one of --check-only, --refresh, --migrate-only")
    print(check_cache.remote() if check_only else download.remote(refresh=refresh, migrate_only=migrate_only))


def run_task(spec, path, *, root=ROOT, output=None, dry_run=False, limit=0):
    name, scene, operation, items = load_task(spec, path, root=root, limit=limit)
    plans = {i["id"]: prepare(spec, i)[1] for i in items}
    prefix = result_prefix(spec, name, scene, operation)
    print(json.dumps({"project": name, "scene": scene, "total": len(items), "prefix": prefix, "kind": spec["kind"]}))
    if dry_run:
        return
    import modal
    from shared.utils.files import file_lock
    from shared.serving.modal_setup import model_volume
    output_root = Path(output) if output is not None else Path("outputs")
    if not output_root.is_absolute():
        output_root = Path(root) / output_root
    folder = output_root / name / scene
    volume = model_volume()
    from shared.tasks.job_client import remote_cache_fingerprint
    cache_fingerprint = remote_cache_fingerprint(spec, volume)
    execution_hashes = {shot: execution_signature(signature, cache_fingerprint) for shot, signature in plans.items()}

    from shared.tasks.job_client import check_record, parallel_collect

    def result_job(record):
        shot = record.get("id")
        if shot not in plans or record.get("status") != "completed":
            raise ValueError("Remote result does not match task/model")
        job = {"id": shot, "project": name, "scene": scene, "prefix": prefix,
               "kind": spec["kind"], "intent_hash": plans[shot],
               "execution_hash": execution_hashes[shot], "result": record}
        check_record(job, record)
        target = folder / record["file"]
        if target.exists() and sha256_file(target) != record["sha256"]:
            raise FileExistsError(target)
        return job

    def collect_results(jobs):
        errors = parallel_collect(jobs, volume, output_root)
        if errors:
            raise errors[0]

    with file_lock(folder):
        pending = []
        completed = []
        for item in items:
            shot = item["id"]
            metadata = folder / ".runner" / "items" / shot / spec["kind"] / "metadata.json"
            existing = [folder / (shot + s) for s in (".png", ".jpg", ".jpeg", ".webp", ".mp4") if (folder / (shot + s)).exists()]
            if existing:
                record = read_json(metadata) if metadata.exists() else {}
                if (len(existing) != 1 or record.get("intent_hash") != plans[shot]
                        or record.get("execution_hash") != execution_hashes[shot]
                        or record.get("file") != existing[0].name or record.get("sha256") != sha256_file(existing[0])):
                    # A client can die after publishing media but before metadata.
                    # Recover only from a matching committed remote result, without GPU.
                    try:
                        recovered = json.loads(b"".join(volume.read_file(f"/{prefix}/{shot}.json")))
                    except FileNotFoundError as exc:
                        raise FileExistsError(f"Existing unverified output: {shot}") from exc
                    if len(existing) != 1:
                        raise FileExistsError(f"Ambiguous output: {shot}")
                    completed.append(result_job(recovered))
                continue
            try:
                record = json.loads(b"".join(volume.read_file(f"/{prefix}/{shot}.json")))
            except FileNotFoundError:
                pending.append(item)
                continue
            if record.get("status") == "completed":
                completed.append(result_job(record))
            else:
                pending.append(item)
        collect_results(completed)
        if pending:
            model = modal.Cls.from_name(spec.get("app_name", "modal-model-" + spec["service"]), spec.get("modal_class", "Model"))()
            collect_results(result_job(record) for record in model.generate_batch_stream.remote_gen(
                pending, prefix, cache_fingerprint, intent_digest(spec)))
