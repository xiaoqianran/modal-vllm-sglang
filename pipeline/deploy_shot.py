"""Deploy with: uv run python -m modal deploy pipeline/deploy_shot.py."""
from pathlib import Path
import json
import os
import modal

from pipeline.s03_run_shot import run
from orchestration.protocol import APP_NAME, state_path
from runtime.serving.modal_setup import model_volume
from runtime.config.storage import VOLUME_MOUNT, VOLUME_SUB_PATH, volume_key
from runtime.tasks.task_identity import intent_digest

root = Path(__file__).resolve().parents[1]
deployment_version = os.environ.get("MODEL_DEPLOY_VERSION", "manual")
# The cloud app also includes local orchestration files mounted copy=False.
# Keep its deployment fingerprint in the image to prevent a silent skipped
# deployment when source-version metadata changes but Python entrypoints do not.
image = (modal.Image.debian_slim(python_version="3.12")
         .pip_install("modal>=1.6.3,<2")
         .env({"PYTHONPATH": "/opt", "MODEL_DEPLOY_VERSION": deployment_version}))
image = image.add_local_dir(str(root / "runtime"), "/opt/runtime", copy=False)
image = image.add_local_dir(str(root / "orchestration"), "/opt/orchestration", copy=False)
image = image.add_local_dir(str(root / "pipeline"), "/opt/pipeline", copy=False)
for config in root.glob("models/*/model.json"):
    image = image.add_local_file(str(config), "/opt/" + config.relative_to(root).as_posix(), copy=False)
    validator = json.loads(config.read_text(encoding="utf-8")).get("validator_module")
    if validator:
        source = Path(*validator.split(".")).with_suffix(".py")
        image = image.add_local_file(str(root / source), "/opt/" + source.as_posix(), copy=False)
app = modal.App(APP_NAME)


def make_provision_info(version):
    @app.function(name="provision_info", serialized=True,
                  image=modal.Image.debian_slim().env({"MODEL_DEPLOY_VERSION": version}))
    def provision_info():
        return version
    return provision_info


provision_info = make_provision_info(deployment_version)


volume = model_volume()
claims = modal.Dict.from_name(APP_NAME + "-claims", create_if_missing=True)


@app.function(image=image, cpu=0.25, memory=512, volumes={VOLUME_MOUNT: volume.with_mount_options(sub_path=VOLUME_SUB_PATH)},
              timeout=86400, retries=0, max_containers=32)
def run_shot(payload, identity):
    # Check durable state first, then use an atomic distributed claim. Volume file
    # locks alone cannot exclude owners running in different Modal containers.
    try:
        existing = json.loads(b"".join(volume.read_file(volume_key("/" + state_path(identity)))))
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if existing["payload_hash"] != intent_digest(payload):
            raise ValueError("Cloud workflow configuration changed")
        return existing
    if not claims.put(identity, intent_digest(payload), skip_if_exists=True):
        raise RuntimeError("Workflow is already claimed; inspect its state instead of replaying")
    return run(payload, identity)
