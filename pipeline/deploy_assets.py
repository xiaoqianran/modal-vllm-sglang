"""Independent CPU-only HF archive app.

Deploy: uv run python -m modal deploy pipeline/deploy_assets.py
Requires an existing Modal secret 'huggingface-secret' containing HF_TOKEN.
"""
import modal

from runtime.config.storage import VOLUME_MOUNT, VOLUME_VERSION, runtime_volume_name
from runtime.config import storage

from runtime.archive.config import ASSET_APP_NAME, archive_source_version, assert_archive_version

image = (modal.Image.debian_slim(python_version="3.12")
         .uv_pip_install("huggingface-hub>=2.4,<3", "pillow>=13", "python-dotenv>=1",
                         index_url="https://pypi.org/simple")
         .env({"ARCHIVE_DEPLOY_VERSION": archive_source_version()})
         .add_local_python_source("runtime", copy=True))
app = modal.App(ASSET_APP_NAME)
# Archive must NEVER create a fresh empty Volume by mistake.
volume = modal.Volume.from_name(
    runtime_volume_name(), create_if_missing=False, version=VOLUME_VERSION)
# The mount presents the model-result namespace at /data regardless of the
# underlying Volume layout. Older volumes have no subdirectory prefix.
sub_path = getattr(storage, "VOLUME_SUB_PATH", "")
source_volume = (volume.with_mount_options(read_only=True, sub_path=sub_path)
                 if sub_path else volume.with_mount_options(read_only=True))


@app.function(image=image, cpu=1, memory=2048, timeout=3600,
              max_containers=4,
              volumes={VOLUME_MOUNT: source_volume},
              secrets=[modal.Secret.from_name("huggingface-secret", required_keys=["HF_TOKEN"])])
def archive_asset(artifact, delivery, project):
    """Volume to HF transfer: no GPU, no PC2 file/download/credentials in payload."""
    from runtime.delivery.cloud_cpu import archive_mounted_artifact
    version = assert_archive_version()
    volume.reload()
    return {**archive_mounted_artifact(VOLUME_MOUNT, artifact, delivery, project),
            "archive_version": version}


@app.function(image=image, cpu=1, memory=2048, timeout=3600,
              max_containers=4,
              volumes={VOLUME_MOUNT: source_volume},
              secrets=[modal.Secret.from_name("huggingface-secret", required_keys=["HF_TOKEN"])])
def archive_batch(requests, expected_version):
    from runtime.delivery.cloud_cpu import archive_mounted_batch
    version = assert_archive_version()
    if expected_version != version:
        raise RuntimeError("Archive deployment version mismatch")
    volume.reload()
    metrics = {}
    results = archive_mounted_batch(VOLUME_MOUNT, requests, metrics=metrics)
    print("ARCHIVE_BATCH", metrics, flush=True)
    return {"archive_version": version, "results": results, "metrics": metrics}


@app.function(image=image, cpu=0.25, memory=512, timeout=60,
              max_containers=1,
              secrets=[modal.Secret.from_name("huggingface-secret", required_keys=["HF_TOKEN"])])
def check_bucket(bucket):
    """Pre-generation remote auth/readiness probe; never mounts GPU or model Volume."""
    from huggingface_hub import HfApi
    from runtime.archive.huggingface import public_url
    import os
    public_url(bucket, "readiness")
    api = HfApi(token=os.environ["HF_TOKEN"])
    identity = api.whoami()
    owner = bucket.split("/", 1)[0]
    if identity.get("name") != owner:
        raise ValueError("Modal HF Secret does not belong to the bucket owner")
    info = api.bucket_info(bucket)
    if getattr(info, "private", False):
        raise ValueError("HF asset bucket must be public")
    return {"bucket": bucket, "ready": True, "archive_version": assert_archive_version()}
