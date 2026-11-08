"""CPU-only MOSS model download; GPU inference requires this cache to be ready."""
import json
import os
import sys
from pathlib import Path

import modal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.cache import file_inventory
from shared.files import atomic_json
from shared.modal_setup import model_volume

MODEL = os.getenv("MOSS_MODEL", "OpenMOSS-Team/MOSS-Image-SFT")
HF_HOME = "/mnt/modal/007-moss-image"  # Existing cache, not a new download location.
ROOT = Path(HF_HOME)
volume = model_volume()
app = modal.App("modal-forge-008-moss-image-download")
image = (modal.Image.debian_slim(python_version="3.11")
         .pip_install("huggingface_hub>=0.30,<2", "hf_xet")
         .env({"HF_HOME": HF_HOME, "PYTHONPATH": "/opt/moss:/opt"})
         .add_local_file(str(Path(__file__).with_name("model_cache.py")),
                         "/opt/moss/model_cache.py", copy=False)
         .add_local_dir(str(Path(__file__).resolve().parent.parent / "shared"),
                        "/opt/shared", copy=False))


@app.function(image=image, cpu=4.0, memory=8192, timeout=86400,
              volumes={"/mnt/modal": volume})
def download():
    from huggingface_hub import snapshot_download

    root = ROOT / "hub"
    root.mkdir(parents=True, exist_ok=True)
    snapshot = Path(snapshot_download(repo_id=MODEL, cache_dir=str(root)))
    inventory = file_inventory(snapshot)
    if not any(name.lower().endswith((".safetensors", ".bin", ".pt")) for name in inventory):
        raise RuntimeError("MOSS download finished without model weights")
    record = {
        "manifest_version": 1,
        "repo_id": MODEL,
        "snapshot_relative": snapshot.relative_to(root).as_posix(),
        "file_count": len(inventory),
        "total_bytes": sum(inventory.values()),
        "inventory": inventory,
    }
    atomic_json(ROOT / "model-ready.json", record)
    volume.commit()
    print(f"CPU READY {MODEL}: {record['file_count']} files, {record['total_bytes']} bytes", flush=True)
    return {k: v for k, v in record.items() if k != "inventory"}


@app.function(image=image, cpu=2.0, memory=4096, timeout=600,
              volumes={"/mnt/modal": volume})
def check_cache():
    from model_cache import get_ready_model
    get_ready_model(MODEL)
    return {"ready": True, "repo_id": MODEL}



@app.local_entrypoint()
def main(check_only: bool = False):
    result = check_cache.remote() if check_only else download.remote()
    print(json.dumps(result, ensure_ascii=False, indent=2))
