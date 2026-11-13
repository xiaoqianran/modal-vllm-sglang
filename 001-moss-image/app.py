"""MOSS-Image on Modal via official vLLM-Omni image."""
import os
import subprocess

import modal
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.modal_setup import model_volume, runtime_image
from model_cache import get_ready_model
from shared.runtime import monitor_processes

MODEL = os.getenv("MOSS_MODEL", "OpenMOSS-Team/MOSS-Image-SFT")
GPU = os.getenv("MOSS_GPU", "RTX-PRO-6000")
app = modal.App("modal-forge-008-moss-image")
volume = model_volume()
image = runtime_image(
    "vllm/vllm-omni:v0.32.0",
    env={"HF_HOME": "/root/.cache/huggingface",
         "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
         "PYTHONPATH": "/opt/moss"},
    files=((Path(__file__).with_name("model_cache.py"), "/opt/moss/model_cache.py"),),
)


@app.function(
    image=image,
    gpu=GPU,
    cpu=4,
    memory=8192,
    volumes={
        "/root/.cache/huggingface": volume.with_mount_options(
            sub_path="/007-moss-image"  # 复用已有权重缓存，避免重新下载
        )
    },
    min_containers=0,
    max_containers=1,
    scaledown_window=2,
    timeout=3600,
)
@modal.concurrent(max_inputs=1)
@modal.web_server(port=8000, startup_timeout=1800, requires_proxy_auth=True)
def serve():
    """Launch official vLLM-Omni MOSS image service."""
    get_ready_model(MODEL)
    process = subprocess.Popen([
        "vllm", "serve", MODEL, "--omni", "--enforce-eager",
        "--host", "0.0.0.0", "--port", "8000",
    ])
    monitor_processes((process,), volume, label="vLLM", commit_interval=60)
