"""Deploy with: uv run python -m modal deploy orchestration/cloud_app.py."""
from pathlib import Path
import json
import modal

from orchestration.cloud import run
from orchestration.protocol import APP_NAME, state_path
from shared.serving.modal_setup import model_volume
from shared.tasks.batch import intent_digest

root = Path(__file__).resolve().parents[1]
image = modal.Image.debian_slim(python_version="3.12").pip_install("modal>=1.6.3,<2").env({"PYTHONPATH": "/opt"})
image = image.add_local_dir(str(root / "shared"), "/opt/shared", copy=False)
image = image.add_local_dir(str(root / "orchestration"), "/opt/orchestration", copy=False)
for config in root.glob("*/model.json"):
    image = image.add_local_file(str(config), "/opt/" + config.relative_to(root).as_posix(), copy=False)
app = modal.App(APP_NAME)
volume = model_volume()
claims = modal.Dict.from_name(APP_NAME + "-claims", create_if_missing=True)


@app.function(image=image, cpu=0.25, memory=512, volumes={"/mnt/modal": volume},
              timeout=86400, retries=0, max_containers=32)
def run_shot(payload, identity):
    # Check durable state first, then use an atomic distributed claim. Volume file
    # locks alone cannot exclude owners running in different Modal containers.
    try:
        existing = json.loads(b"".join(volume.read_file("/" + state_path(identity))))
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if existing["payload_hash"] != intent_digest(payload):
            raise ValueError("Cloud workflow configuration changed")
        return existing
    if not claims.put(identity, intent_digest(payload), skip_if_exists=True):
        raise RuntimeError("Workflow is already claimed; inspect its state instead of replaying")
    return run(payload, identity)
