"""One deployed Pika GPU Class shared by the HTTP proxy and SDK video batches."""
import hashlib
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import modal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.modal_setup import model_volume, runtime_image
from shared.runtime import stop_process, wait_for_health
from model_cache import READY_FILE, get_ready_model
from video import SG_PORT, PROXY_PORT, SGLANG_FLAGS, generate_batch as execute_batch

GPU = os.getenv("PIKA_GPU", "RTX-PRO-6000")
app = modal.App("modal-forge-009-pika-h3-flex-fl2va")
volume = model_volume()
image = runtime_image(
    "lmsysorg/sglang:dev",
    env={"HF_HOME": "/mnt/modal/009-pika-h3-flex",
         "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
         "SGLANG_DIFFUSION_CACHE_ROOT": "/mnt/modal/009-pika-h3-flex/sglang",
         "PYTHONPATH": "/opt/pika"},
    files=(
        (Path(__file__).with_name("proxy.py"), "/opt/pika/proxy.py"),
        (Path(__file__).with_name("video.py"), "/opt/pika/video.py"),
        (Path(__file__).with_name("model_cache.py"), "/opt/pika/model_cache.py"),
    ),
)


@app.cls(
    image=image, gpu=GPU, cpu=4, memory=49152,
    volumes={"/mnt/modal": volume},
    min_containers=0, max_containers=1, scaledown_window=30,
    startup_timeout=3600, timeout=14400,
)
@modal.concurrent(max_inputs=2)
class Pika:
    @modal.enter()
    def start(self):
        started = time.perf_counter()
        model = get_ready_model()
        self.manifest_sha256 = hashlib.sha256(READY_FILE.read_bytes()).hexdigest()
        self.engine = subprocess.Popen([
            "python", "-m", "sglang.multimodal_gen.runtime.entrypoints.cli.main",
            "serve", "--model-path", model,
            "--host", "127.0.0.1", "--port", str(SG_PORT),
            *SGLANG_FLAGS,
        ])
        self.gateway = None
        try:
            wait_for_health(self.engine, f"http://127.0.0.1:{SG_PORT}/health",
                            timeout=3300, interval=3)
            self.gateway = subprocess.Popen(["python", "-u", "/opt/pika/proxy.py"])
            wait_for_health(self.gateway, f"http://127.0.0.1:{PROXY_PORT}/health",
                            timeout=120, interval=2)
        except BaseException:
            if self.gateway is not None:
                stop_process(self.gateway)
            stop_process(self.engine)
            raise
        self.startup_seconds = round(time.perf_counter() - started, 2)
        self.batch_lock = threading.Lock()
        print(f"Pika GPU ready in {self.startup_seconds}s: {GPU}", flush=True)

    @modal.web_server(port=PROXY_PORT,
                      label="modal-forge-009-pika-h3-flex-fl2va-serve",
                      startup_timeout=3600, requires_proxy_auth=True)
    def serve(self):
        """Forward HTTP requests to the same SGLang engine."""
        pass

    @modal.method()
    def generate_batch(self, workflow: dict, items: list[dict], output_prefix: str):
        """Reuse the model already running in this Class container."""
        with self.batch_lock:
            return execute_batch(
                workflow, items, output_prefix,
                volume=volume,
                model_manifest_sha256=self.manifest_sha256,
                startup_seconds=self.startup_seconds,
            )

    @modal.exit()
    def stop(self):
        if getattr(self, "gateway", None) is not None:
            stop_process(self.gateway)
        if getattr(self, "engine", None) is not None:
            stop_process(self.engine)
        volume.commit()
