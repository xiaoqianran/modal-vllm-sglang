"""MiniMax H3 Image on SGLang; independent Modal deployment."""
import os
import subprocess
import sys
from pathlib import Path

import modal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from model_cache import HF_HOME, SGLANG_REVISION, get_ready_model
from image import generate_batch as execute_batch
from shared.modal_setup import model_volume
from shared.runtime import stop_process, wait_for_health

GPU = os.getenv("MINIMAX_GPU", "RTX-PRO-6000")
SNAPSHOT = os.getenv("MINIMAX_GPU_SNAPSHOT", "0")
if SNAPSHOT not in ("0", "1"):
    raise ValueError("MINIMAX_GPU_SNAPSHOT must be 0 or 1")
GPU_SNAPSHOT = SNAPSHOT == "1"
PORT = 30000
app = modal.App("modal-forge-005-minimax-h3-image")
volume = model_volume()
image = modal.Image.from_registry(
    os.getenv("MINIMAX_IMAGE", "lmsysorg/sglang@sha256:7d2f322fbe8eda16ee65f969f8c9c2be602ecb0a29bf3c8b219945432f3992b2")
).entrypoint([]).run_commands(
    f"python -m pip install 'sglang[diffusion] @ git+https://github.com/sgl-project/sglang.git@{SGLANG_REVISION}#subdirectory=python'"
).env({"HF_HOME": HF_HOME, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
       "PYTHONPATH": "/opt/minimax:/opt"})
image = image.add_local_file(str(Path(__file__).with_name("model_cache.py")),
                             "/opt/minimax/model_cache.py", copy=False)
image = image.add_local_file(str(Path(__file__).with_name("image.py")),
                             "/opt/minimax/image.py", copy=False)
image = image.add_local_dir(str(Path(__file__).resolve().parent.parent / "shared"),
                            "/opt/shared", copy=False)


@app.cls(image=image, gpu=GPU, cpu=12, memory=262144,
         volumes={"/mnt/modal": volume}, min_containers=0, max_containers=1,
         scaledown_window=30, timeout=3600,
         enable_memory_snapshot=GPU_SNAPSHOT,
         experimental_options={"enable_gpu_snapshot": True} if GPU_SNAPSHOT else None)
@modal.concurrent(max_inputs=1)
class H3:
    @modal.enter(snap=GPU_SNAPSHOT)
    def start(self):
        model, encoder = get_ready_model()
        self.process = subprocess.Popen([
            "sglang", "serve", "--model-path", model, "--text-encoder-path", encoder,
            "--host", "0.0.0.0", "--port", str(PORT), "--warmup-mode", "off",
        ])
        try:
            wait_for_health(self.process, f"http://127.0.0.1:{PORT}/health", timeout=3500)
        except BaseException:
            stop_process(self.process)
            raise

    @modal.enter(snap=False)
    def check_restored(self):
        wait_for_health(self.process, f"http://127.0.0.1:{PORT}/health", timeout=120)

    @modal.web_server(port=PORT, label="modal-forge-005-minimax-h3-image-serve",
                      startup_timeout=3600, requires_proxy_auth=True)
    def serve(self):
        pass

    @modal.method()
    def generate_batch(self, items: list[dict], output_prefix: str):
        """Use the same already initialized SGLang instance as private HTTP."""
        return execute_batch(items, output_prefix, volume=volume, port=PORT)

    @modal.exit()
    def stop(self):
        if getattr(self, "process", None) is not None:
            stop_process(self.process)
        volume.commit()
