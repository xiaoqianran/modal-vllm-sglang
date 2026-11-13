"""Download only the required pinned components on CPU."""
import json
import sys
from pathlib import Path

import modal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from model_cache import HF_HOME, SPECS
from shared.cache import file_inventory
from shared.files import atomic_json
from shared.modal_setup import model_volume

volume = model_volume()
app = modal.App("modal-forge-003-minimax-h3-image-download")
image = (modal.Image.debian_slim(python_version="3.11")
         .pip_install("huggingface_hub>=0.30,<2", "hf_xet")
         .env({"HF_HOME": HF_HOME, "PYTHONPATH": "/opt/minimax:/opt"})
         .add_local_file(str(Path(__file__).with_name("model_cache.py")), "/opt/minimax/model_cache.py", copy=False)
         .add_local_dir(str(Path(__file__).resolve().parent.parent / "shared"), "/opt/shared", copy=False))


@app.function(image=image, cpu=4, memory=8192, timeout=86400, volumes={"/mnt/modal": volume})
def download():
    from huggingface_hub import snapshot_download
    from model_cache import get_ready_model

    root = Path(HF_HOME)
    record = {"manifest_version": 1}
    for name, (repo, revision, patterns) in SPECS.items():
        path = Path(snapshot_download(repo_id=repo, revision=revision,
                    allow_patterns=patterns, cache_dir=str(root / "hub")))
        record[name] = {"repo_id": repo, "revision": revision,
                        "snapshot_relative": path.relative_to(root / "hub").as_posix(),
                        "inventory": file_inventory(path)}
    atomic_json(root / "model-ready.json", record)
    get_ready_model()
    volume.commit()
    return {"ready": True, "components": list(SPECS)}


@app.function(image=image, cpu=2, memory=4096, timeout=600, volumes={"/mnt/modal": volume})
def check_cache():
    from model_cache import get_ready_model
    model, encoder = get_ready_model()
    return {"ready": True, "model": model, "encoder": encoder}


@app.local_entrypoint()
def main(check_only: bool = False):
    print(json.dumps(check_cache.remote() if check_only else download.remote(), indent=2))
