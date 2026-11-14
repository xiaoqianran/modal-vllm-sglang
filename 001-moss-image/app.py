"""MOSS-Image: one deployed GPU Class, shared by private HTTP and SDK batches."""
import os
import subprocess
import sys
import time
from pathlib import Path

import modal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.modal_setup import model_volume, runtime_image
from shared.runtime import stop_process, wait_for_health
from model_cache import get_ready_model
from image import stream_batch

MODEL = os.getenv("MOSS_MODEL", "OpenMOSS-Team/MOSS-Image-SFT")
GPU = os.getenv("MOSS_GPU", "RTX-PRO-6000")
PORT = 8000
app = modal.App("modal-forge-008-moss-image")
volume = model_volume()
image = runtime_image(
    "vllm/vllm-omni:v0.32.0",
    env={"HF_HOME": "/mnt/modal/007-moss-image",
         "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
         "PYTHONPATH": "/opt/moss"},
    files=(
        (Path(__file__).with_name("model_cache.py"), "/opt/moss/model_cache.py"),
        (Path(__file__).with_name("image.py"), "/opt/moss/image.py"),
    ),
)


@app.cls(
    image=image, gpu=GPU, cpu=4, memory=8192,
    volumes={"/mnt/modal": volume},
    min_containers=0, max_containers=1, scaledown_window=2,
    startup_timeout=1800, timeout=14400,
)
@modal.concurrent(max_inputs=1)
class Moss:
    @modal.enter()
    def start(self):
        """Load the model once per GPU container for both HTTP and SDK inputs."""
        started = time.perf_counter()
        get_ready_model(MODEL)
        self.process = subprocess.Popen([
            "vllm", "serve", MODEL, "--omni", "--enforce-eager",
            "--host", "0.0.0.0", "--port", str(PORT),
        ])
        try:
            wait_for_health(self.process, f"http://127.0.0.1:{PORT}/health",
                            timeout=1500, interval=3)
        except BaseException:
            stop_process(self.process)
            raise
        self.startup_seconds = round(time.perf_counter() - started, 2)

    @modal.web_server(port=PORT, label="modal-forge-008-moss-image-serve",
                      startup_timeout=1800, requires_proxy_auth=True)
    def serve(self):
        """Expose the same local vLLM server; do not start another process."""
        pass

    @modal.method()
    def generate_batch_stream(self, shots: list[dict], output_prefix: str):
        """Stream images from this container's already loaded vLLM process."""
        yield from stream_batch(shots, output_prefix, volume, self.startup_seconds)

    @modal.exit()
    def stop(self):
        if getattr(self, "process", None) is not None:
            stop_process(self.process)
        volume.commit()
