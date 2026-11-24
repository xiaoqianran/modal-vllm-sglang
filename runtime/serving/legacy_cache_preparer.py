"""CPU model-cache preparation and download CLI."""
import json
import os
from pathlib import Path

from runtime.utils.files import read_json
from runtime.config.model_spec import validate
from runtime.config.storage import runtime_volume_name


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
    """Register module-level remote workers; config travels as plain data."""
    import modal
    from runtime.serving.modal_setup import model_mounts
    from runtime.serving.remote_workers import (
        legacy_prepare_cache_entry,
        legacy_check_cache_entry,
        provision_info_entry,
    )

    spec = validate(read_json(config_path))
    if spec.get("cache", {}).get("mode") == "sglang_overlay":
        raise ValueError("Overlay models must use tools/download_models.py --execute; "
                         "the legacy single-container entrypoint is disabled")

    volume_name = runtime_volume_name()
    app = modal.App("prepare-" + spec["service"])
    deployment_version = os.environ.get("MODEL_DEPLOY_VERSION", "manual")
    provision_image = (
        modal.Image.debian_slim(python_version="3.12")
        .env({"MODEL_DEPLOY_VERSION": deployment_version})
        .add_local_python_source("runtime")
    )
    app.function(
        name="provision_info", image=provision_image, serialized=False,
        cpu=0.25, memory=512, timeout=60,
    )(provision_info_entry)

    image = (
        modal.Image.debian_slim(python_version="3.12")
        .uv_pip_install("huggingface_hub>=0.30,<2", "hf_xet")
        .env({
            "PYTHONPATH": "/opt",
            "MODEL_DEPLOY_VERSION": deployment_version,
            "MODEL_VOLUME_NAME": volume_name,
            "MODEL_CACHE_SPEC_JSON": json.dumps(spec, ensure_ascii=True),
        })
        .add_local_dir(str(Path(__file__).parent.parent), "/opt/runtime", copy=False, ignore=['**/__pycache__/**', '**/*.pyc', '**/*.pyo'])
    )
    mounts = model_mounts(spec)
    download = app.function(
        name="download", image=image, serialized=False, cpu=4, memory=8192,
        timeout=86400, max_containers=1, volumes=mounts,
        secrets=optional_hf_download_secrets(),
    )(legacy_prepare_cache_entry)
    check_cache = app.function(
        name="check_cache", image=image, serialized=False, cpu=2, memory=8192,
        timeout=600, volumes=mounts,
    )(legacy_check_cache_entry)
    return app, download, check_cache


def run_download_cli(download, check_cache, *, check_only=False, refresh=False, migrate_only=False):
    if sum((check_only, refresh, migrate_only)) > 1:
        raise ValueError("Choose only one of --check-only, --refresh, --migrate-only")
    print(check_cache.remote() if check_only else download.remote(refresh=refresh, migrate_only=migrate_only))
