"""Isolated deployment: modal deploy experiments/image_studio/deploy.py."""
import io
import json
import os
from pathlib import Path
import modal

ROOT = Path(__file__).resolve().parents[2]
app = modal.App("image-studio-preview")
image = (modal.Image.debian_slim(python_version="3.12")
         .pip_install("fastapi>=0.115,<1", "httpx>=0.28,<1", "pillow>=11,<14")
         .add_local_dir(Path(__file__).parent, "/opt/image_studio",
                        ignore=["**/__pycache__/**", "**/*.pyc"])
         .env({"PYTHONPATH": "/opt/image_studio:/opt"}))
for package in ("runtime", "orchestration", "pipeline"):
    image = image.add_local_dir(str(ROOT / package), "/opt/" + package, copy=False)
for config_path in ROOT.glob("models/*/model.json"):
    image = image.add_local_file(str(config_path), "/opt/" + config_path.relative_to(ROOT).as_posix(), copy=False)
    module = json.loads(config_path.read_text(encoding="utf-8")).get("validator_module")
    if module:
        validator = Path(*module.split(".")).with_suffix(".py")
        image = image.add_local_file(str(ROOT / validator), "/opt/" + validator.as_posix(), copy=False)
volume = modal.Volume.from_name("image-studio-preview-private", create_if_missing=True, version=2)
config = modal.Secret.from_name("image-studio-preview")


class VolumeStore:
    def write(self, key, value):
        with volume.batch_upload(force=True) as batch:
            batch.put_file(io.BytesIO(value), "/" + key)

    def read(self, key):
        return b"".join(volume.read_file("/" + key))


@app.function(image=image, secrets=[config], timeout=1500, retries=0, max_containers=1)
def render(job):
    from studio import SDKH3, run_job
    engine = SDKH3()
    run_job(VolumeStore(), engine, job)


@app.function(image=image, secrets=[config], timeout=60, max_containers=1)
@modal.asgi_app(label="image-studio-preview")
def web():
    from studio import create_app
    return create_app(VolumeStore(), render.spawn, os.environ.get("IMAGE_STUDIO_PASSWORD"))
