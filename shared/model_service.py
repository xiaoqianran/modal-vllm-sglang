"""Standard deployed image/video service, CPU cache preparation and SDK CLI.

Engine-specific behavior belongs in model.json's argv and image. This module
targets /v1/images/generations and /v1/videos, including inline keyframes.
"""
import argparse
import json
import base64
import hashlib
import subprocess
import threading
from pathlib import Path

from shared.batch import completed_output, intent_digest
from shared.files import atomic_json, download_volume_file, read_json, sha256_file
from shared.media import image_format
from shared.model_spec import parameters, validate, operation_spec, operations
from shared.model_cache import cache_root, cache_directory, verify_cache, prepare_cache, command_argv, component_specs
from shared.outputs import component, record_artifact
from shared.task_lifecycle import recovery_action, TERMINAL, SubmissionRejected

ROOT = Path(__file__).resolve().parent.parent


def class_name(name):
    def decorate(cls):
        cls.__name__ = name
        return cls
    return decorate


def media_suffix(data, kind):
    if kind == "image":
        return image_format(data)[0]
    if len(data) >= 32 and data[4:8] == b"ftyp":
        return ".mp4"
    raise ValueError("Expected MP4 content")


def prepare(spec, item):
    component(item["id"])
    if not isinstance(item.get("prompt"), str) or not item["prompt"].strip():
        raise ValueError("Nonempty prompt required")
    if type(item.get("seed")) is not int or not 0 <= item["seed"] < 2**32:
        raise ValueError("uint32 seed required")
    operation = item.get("operation", spec["operation"])
    selected = operation_spec(spec, operation)
    payload = parameters(spec, item.get("params", {}), operation)
    payload.update(prompt=item["prompt"], seed=item["seed"])
    for field in ("first_frame", "last_frame"):
        value = item.get(field + "_base64")
        required = field in selected["inputs"]
        if required != (value is not None):
            raise ValueError(f"{field} does not match operation {operation}")
        if required:
            from shared.inference_gateway import _decode_frame
            _decode_frame(value)
            payload[field + "_base64"] = value
    return payload, intent_digest({"spec": spec, "operation": operation, "payload": payload})


def execute(spec, items, prefix, volume, cache_fingerprint="unverified"):
    from shared.inference_api import VLLMImageClient, SGLangVideoClient
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
        metadata = folder / (item["id"] + ".json")
        old = read_json(metadata) if metadata.exists() else {}
        if old.get("intent_hash") not in (None, signature):
            raise ValueError("Remote task signature changed")
        execution_hash = intent_digest({"intent": signature, "cache": cache_fingerprint})
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
            # Persist before submit: a lost response never causes automatic resubmission.
            atomic_json(metadata, {"status": "submitting", "intent_hash": signature, "execution_hash": execution_hash})
            volume.commit()
            try:
                if spec["kind"] == "image":
                    data, _ = client.generate(payload, timeout=spec.get("timeout", 3600))
                else:
                    job = client.submit(payload, timeout=spec.get("timeout", 3600))
                    atomic_json(metadata, {"status": "running", "intent_hash": signature, "execution_hash": execution_hash, "job_id": job})
                    volume.commit()
            except SubmissionRejected:
                atomic_json(metadata, {"status": "pending", "intent_hash": signature, "execution_hash": execution_hash})
                volume.commit()
                raise
        if spec["kind"] == "video":
            def remember_failure(remote):
                if remote.get("status") in TERMINAL - {"completed"}:
                    record = read_json(metadata)
                    record["status"] = "failed"
                    atomic_json(metadata, record)
                    volume.commit()
            client.wait(job, timeout=spec.get("timeout", 3600), on_status=remember_failure)
            data = client.content(job)
        suffix = media_suffix(data, spec["kind"])
        target = folder / (item["id"] + suffix)
        digest = hashlib.sha256(data).hexdigest()
        if target.exists() and sha256_file(target) != digest:
            raise FileExistsError(f"Refusing overwrite: {target}")
        record = {"id": item["id"], "file": target.name, "status": "collecting", "bytes": len(data),
                  "intent_hash": signature, "execution_hash": execution_hash, "sha256": digest}
        if spec["kind"] == "video":
            record["job_id"] = job
        atomic_json(metadata, record)
        volume.commit()
        from shared.files import atomic_write
        if not target.exists():
            atomic_write(target, data)
        record["status"] = "completed"
        atomic_json(metadata, record)
        volume.commit()
        yield record


def build_image(spec, *, offline):
    from shared.modal_setup import runtime_image
    root = cache_directory(spec)
    env = {**spec.get("env", {}), "HF_HOME": root,
           "SGLANG_DIFFUSION_CACHE_ROOT": root + "/sglang"}
    if offline:
        env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    else:
        env.update(HF_HUB_OFFLINE="0", TRANSFORMERS_OFFLINE="0")
    return runtime_image(spec["image"], env=env, commands=spec.get("build_commands", ()))


def create_app(config_path):
    """One GPU lifecycle shared by all configured models."""
    import modal
    from shared.modal_setup import model_volume
    from shared.runtime import stop_process, wait_for_health
    config_path = Path(config_path)
    spec = validate(read_json(config_path))
    volume = model_volume()
    app = modal.App(spec.get("app_name", "modal-model-" + spec["service"]))
    image = build_image(spec, offline=True).add_local_file(str(config_path), "/opt/model.json", copy=False)
    startup = spec.get("startup_timeout", 3600)

    @app.cls(image=image, serialized=True, gpu=spec.get("gpu", "RTX-PRO-6000"),
             cpu=spec.get("cpu", 4), memory=spec.get("memory", 32768),
             volumes={"/mnt/modal": volume}, min_containers=0, max_containers=1,
             scaledown_window=spec.get("scaledown_window", 30),
             startup_timeout=startup, timeout=spec.get("timeout", 14400))
    @modal.concurrent(max_inputs=8)
    @class_name(spec.get("modal_class", "Model"))
    class Model:
        @modal.enter()
        def start(self):
            self.batch_lock = threading.Lock()
            paths, self.cache_fingerprint = verify_cache(spec)
            self.process = subprocess.Popen(command_argv(spec, paths))
            self.gateway = None
            try:
                wait_for_health(self.process, f"http://127.0.0.1:{spec.get('port', 8000)}/health", timeout=startup - 60)
                self.gateway = subprocess.Popen(["python", "-m", "shared.inference_gateway", "--config", "/opt/model.json"])
                port = spec.get("proxy_port", spec.get("port", 8000) + 1)
                wait_for_health(self.gateway, f"http://127.0.0.1:{port}/health", timeout=120)
            except BaseException:
                if self.gateway is not None:
                    stop_process(self.gateway)
                stop_process(self.process)
                raise

        @modal.web_server(port=spec.get("proxy_port", spec.get("port", 8000) + 1),
                          label=spec.get("endpoint_label", spec["service"] + "-serve"), startup_timeout=startup, requires_proxy_auth=True)
        def serve(self):
            pass

        @modal.method()
        def generate_batch_stream(self, items, prefix, expected_cache_fingerprint, expected_spec_hash):
            if expected_spec_hash != intent_digest(spec):
                raise RuntimeError("Local configuration differs from deployed model.json; deploy it first")
            if expected_cache_fingerprint != self.cache_fingerprint:
                raise RuntimeError("Prepared model changed; restart with the current cache")
            with self.batch_lock:
                yield from execute(spec, items, prefix, volume, self.cache_fingerprint)

        @modal.exit()
        def stop(self):
            for name in ("gateway", "process"):
                if getattr(self, name, None) is not None:
                    stop_process(getattr(self, name))
            volume.commit()

    return app, Model


def create_download_app(config_path):
    import modal
    from shared.modal_setup import model_volume
    spec = validate(read_json(config_path))
    volume = model_volume()
    app = modal.App("prepare-" + spec["service"])
    if spec.get("cache", {}).get("mode") == "sglang_overlay":
        image = build_image(spec, offline=False)
    else:
        image = modal.Image.debian_slim(python_version="3.11").pip_install("huggingface_hub>=0.30,<2", "hf_xet")
        image = image.env({"PYTHONPATH": "/opt"}).add_local_dir(str(Path(__file__).parent), "/opt/shared", copy=False)

    @app.function(name="download", image=image, serialized=True, cpu=4, memory=8192,
                  timeout=86400, volumes={"/mnt/modal": volume})
    def download(refresh: bool = False):
        return prepare_cache(spec, volume, refresh=refresh)

    @app.function(name="check_cache", image=image, serialized=True, cpu=2, memory=8192,
                  timeout=600, volumes={"/mnt/modal": volume})
    def check_cache():
        _, fingerprint = verify_cache(spec)
        return {"ready": True, "fingerprint": fingerprint}

    return app, download, check_cache


def load_task(spec, path, *, root=ROOT, limit=0):
    path = Path(path)
    task = read_json(path if path.is_absolute() else Path(root) / path)
    allowed = {"version", "name", "project", "scene", "workflow", "params", "items"}
    if not isinstance(task, dict) or task.keys() - allowed or type(task.get("version")) is not int or task["version"] != 1:
        raise ValueError("Expected version-1 task JSON")
    component(task["name"])
    project, scene = component(task.get("project", task["name"])), component(task.get("scene", "001-main"))
    candidates = [op for op in operations(spec) if task.get("workflow") == spec["service"] + "-" + op]
    if len(candidates) != 1:
        raise ValueError("Task workflow does not belong to this model")
    operation = candidates[0]
    selected = operation_spec(spec, operation)
    if not isinstance(task.get("params", {}), dict) or not isinstance(task.get("items"), list) or not task["items"]:
        raise ValueError("Task requires params object and nonempty items")
    items, seen = [], set()
    for raw in task["items"]:
        if not isinstance(raw, dict) or raw.keys() - {"id", "prompt", "seed", "params", "first_frame", "last_frame"}:
            raise ValueError("Unsupported item fields")
        shot = component(raw["id"])
        if shot.lower() in seen:
            raise ValueError("Duplicate shot ID")
        seen.add(shot.lower())
        if not isinstance(raw.get("params", {}), dict):
            raise ValueError("Item params must be an object")
        seed = int.from_bytes(hashlib.sha256(f"{task['name']}/{operation}/{shot}".encode()).digest()[:4], "big") & 0x7fffffff
        item = {"id": shot, "operation": operation, "prompt": raw["prompt"], "seed": raw.get("seed", seed),
                "params": {**task.get("params", {}), **raw.get("params", {})}}
        for field in ("first_frame", "last_frame"):
            required = field in selected["inputs"]
            if required != (field in raw):
                raise ValueError(f"{field} does not match operation")
            if required:
                binding = raw[field]
                if not isinstance(binding, dict) or set(binding) != {"file"}:
                    raise ValueError("Frame input must be {file: repo-relative-path}")
                if not isinstance(binding["file"], str) or Path(binding["file"]).is_absolute():
                    raise ValueError("Frame path must be repository relative")
                from shared.files import local_path
                # Input identity is its content, never its local path.
                frame = local_path(root, binding["file"])
                if not frame.is_file() or not 0 < frame.stat().st_size <= 20 * 1024 * 1024:
                    raise ValueError("Frame must be 1 byte to 20 MiB")
                data = frame.read_bytes()
                image_format(data)
                item[field + "_base64"] = base64.b64encode(data).decode("ascii")
        prepare(spec, item)  # Validate the full task, even when --limit selects fewer items.
        items.append(item)
    return project, scene, operation, items[:limit] if limit else items


def run_task(spec, path, *, root=ROOT, output=None, dry_run=False, limit=0):
    name, scene, operation, items = load_task(spec, path, root=root, limit=limit)
    plans = {i["id"]: prepare(spec, i)[1] for i in items}
    prefix = f"model-results/{spec['service']}/{name}/{scene}/{operation}-{intent_digest(spec)[:20]}"
    print(json.dumps({"project": name, "scene": scene, "total": len(items), "prefix": prefix, "kind": spec["kind"]}))
    if dry_run:
        return
    import modal
    from shared.files import file_lock
    from shared.modal_setup import model_volume
    output_root = Path(output) if output is not None else Path("outputs")
    if not output_root.is_absolute():
        output_root = Path(root) / output_root
    folder = output_root / name / scene
    volume = model_volume()
    try:
        remote_cache = cache_directory(spec).removeprefix("/mnt/modal") + "/model-ready.json"
        cache_record = json.loads(b"".join(volume.read_file(remote_cache)))
    except FileNotFoundError as exc:
        raise RuntimeError("Prepare the model on CPU with download_model.py first") from exc
    if cache_record.get("version") != 1 or cache_record.get("cache_spec") != intent_digest(component_specs(spec)):
        raise RuntimeError("Prepared cache differs from model.json; prepare it again on CPU with --refresh")
    cache_fingerprint = intent_digest(cache_record)
    execution_hashes = {shot: intent_digest({"intent": signature, "cache": cache_fingerprint}) for shot, signature in plans.items()}

    def receive(record):
        shot = record["id"]
        filename = record["file"]
        if (shot not in plans or record.get("status") != "completed" or record.get("intent_hash") != plans[shot]
                or record.get("execution_hash") != execution_hashes[shot]
                or filename not in {shot + s for s in (".png", ".jpg", ".webp", ".mp4")}):
            raise ValueError("Remote result does not match task/model")
        target = folder / filename
        if target.exists():
            if sha256_file(target) != record["sha256"]:
                raise FileExistsError(target)
        else:
            download_volume_file(volume, f"/{prefix}/{filename}", target,
                                 expected_sha256=record["sha256"], kind=spec["kind"], refuse_existing=True)
        atomic_json(folder / ".runner" / "items" / shot / spec["kind"] / "metadata.json", record)
        record_artifact(folder, target, spec["kind"], source=f"/{prefix}/{filename}")

    with file_lock(folder):
        pending = []
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
                    receive(recovered)
                continue
            try:
                record = json.loads(b"".join(volume.read_file(f"/{prefix}/{shot}.json")))
            except FileNotFoundError:
                pending.append(item)
                continue
            if record.get("status") == "completed":
                receive(record)
            else:
                pending.append(item)
        if pending:
            model = modal.Cls.from_name(spec.get("app_name", "modal-model-" + spec["service"]), spec.get("modal_class", "Model"))()
            for record in model.generate_batch_stream.remote_gen(pending, prefix, cache_fingerprint, intent_digest(spec)):
                receive(record)


def run_cli(config_path):
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True, action="append", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("limit must be nonnegative")
    spec = validate(read_json(config_path))
    destinations = set()
    for path in args.task:
        project, scene, _, _ = load_task(spec, path, limit=args.limit)
        if (project.lower(), scene.lower()) in destinations:
            raise ValueError("Repeated project/scene across task files")
        destinations.add((project.lower(), scene.lower()))
    for path in args.task:
        run_task(spec, path, output=args.output, dry_run=args.dry_run, limit=args.limit)
