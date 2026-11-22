"""CPU model-cache preparation and download CLI."""
import os
from pathlib import Path
from shared.utils.files import read_json
from shared.config.model_spec import validate
from shared.config.model_cache import verify_cache, prepare_cache
from shared.serving.model_runtime import build_image


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
    from shared.serving.modal_setup import model_volume, model_mounts
    spec = validate(read_json(config_path))
    volume = model_volume()
    app = modal.App("prepare-" + spec["service"])
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
                  timeout=86400, max_containers=1, volumes=model_mounts(spec),
                  secrets=optional_hf_download_secrets())
    def download(refresh: bool = False, migrate_only: bool = False, repair: bool = False):
        if repair and not refresh and not migrate_only:
            try:
                return prepare_cache(spec, volume)
            except RuntimeError:
                return prepare_cache(spec, volume, refresh=True)
        return prepare_cache(spec, volume, refresh=refresh, migrate_only=migrate_only)

    @app.function(name="check_cache", image=image, serialized=True, cpu=2, memory=8192,
                  timeout=600, volumes=model_mounts(spec))
    def check_cache():
        _, fingerprint = verify_cache(spec)
        return {"ready": True, "fingerprint": fingerprint}

    return app, download, check_cache


def run_download_cli(download, check_cache, *, check_only=False, refresh=False, migrate_only=False):
    if sum((check_only, refresh, migrate_only)) > 1:
        raise ValueError("Choose only one of --check-only, --refresh, --migrate-only")
    print(check_cache.remote() if check_only else download.remote(refresh=refresh, migrate_only=migrate_only))
