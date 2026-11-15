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


from shared.serving.model_runtime import class_name, build_image, create_app
from shared.serving.inference_execution import execute, publish_telemetry


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
    import os
    deployment_version = os.environ.get("MODEL_DEPLOY_VERSION", "manual")

    @app.function(name="provision_info", image=modal.Image.debian_slim().env({"MODEL_DEPLOY_VERSION": deployment_version}), serialized=True)
    def provision_info():
        return deployment_version
    if spec.get("cache", {}).get("mode") == "sglang_overlay":
        image = build_image(spec, offline=False)
    else:
        image = modal.Image.debian_slim(python_version="3.12").pip_install("huggingface_hub>=0.30,<2", "hf_xet")
        image = image.env({"PYTHONPATH": "/opt", "MODEL_DEPLOY_VERSION": os.environ.get("MODEL_DEPLOY_VERSION", "manual")}).add_local_dir(str(Path(__file__).parent.parent), "/opt/shared", copy=False)

    @app.function(name="download", image=image, serialized=True, cpu=4, memory=8192,
                  timeout=86400, max_containers=1, volumes={"/mnt/modal": volume},
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
