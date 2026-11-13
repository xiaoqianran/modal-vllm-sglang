"""MiniMax H3 Image on SGLang; independent Modal deployment."""
import os
import subprocess
import sys
from pathlib import Path

import modal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from model_cache import HF_HOME, get_ready_model
from shared.modal_setup import model_volume
from shared.runtime import monitor_processes

# Source revision from SGLang's measured RTX PRO 6000 recipe.
SGLANG_REVISION = "a9d721f36e16dab1835de0203b620256da1e612d"
GPU = os.getenv("MINIMAX_GPU", "RTX-PRO-6000")
app = modal.App("modal-forge-003-minimax-h3-image")
volume = model_volume()
image = modal.Image.from_registry(
    os.getenv("MINIMAX_IMAGE", "lmsysorg/sglang@sha256:7d2f322fbe8eda16ee65f969f8c9c2be602ecb0a29bf3c8b219945432f3992b2")
).entrypoint([]).run_commands(
    f"python -m pip install 'sglang[diffusion] @ git+https://github.com/sgl-project/sglang.git@{SGLANG_REVISION}#subdirectory=python'"
).env({"HF_HOME": HF_HOME, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
       "PYTHONPATH": "/opt/minimax:/opt"})
image = image.add_local_file(str(Path(__file__).with_name("model_cache.py")),
                             "/opt/minimax/model_cache.py", copy=False)
image = image.add_local_dir(str(Path(__file__).resolve().parent.parent / "shared"),
                            "/opt/shared", copy=False)


@app.function(image=image, gpu=GPU, cpu=12, memory=262144,
              volumes={"/mnt/modal": volume}, min_containers=0, max_containers=1,
              scaledown_window=120, timeout=3600)
@modal.concurrent(max_inputs=1)
@modal.web_server(port=30000, startup_timeout=3600, requires_proxy_auth=True)
def serve():
    model, encoder = get_ready_model()
    process = subprocess.Popen([
        "sglang", "serve", "--model-path", model, "--text-encoder-path", encoder,
        "--host", "0.0.0.0", "--port", "30000", "--warmup-mode", "off",
    ])
    monitor_processes((process,), volume, label="MiniMax H3 Image")
