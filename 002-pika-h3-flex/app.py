"""Pika H3 Flex SGLang Diffusion + inline FL2VA frame proxy on Modal."""
import os
import subprocess
import threading
import time
from pathlib import Path

import modal
from model_cache import get_ready_model

MODEL = "Pika-Labs/Pika-H3-Flex"
GPU = os.getenv("PIKA_GPU", "RTX-PRO-6000")
SG_PORT = 30010
PROXY_PORT = 30011

app = modal.App("modal-forge-009-pika-h3-flex-fl2va")
volume = modal.Volume.from_name("modal-forge", create_if_missing=True)
image = (
    modal.Image.from_registry("lmsysorg/sglang:dev")
    .entrypoint([])
    .env({"HF_HOME": "/mnt/modal/009-pika-h3-flex",
          "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
          "SGLANG_DIFFUSION_CACHE_ROOT": "/mnt/modal/009-pika-h3-flex/sglang",
          "PYTHONPATH": "/opt/pika"})
    .add_local_file(str(Path(__file__).with_name("proxy.py")), "/opt/pika/proxy.py")
    .add_local_file(str(Path(__file__).with_name("model_cache.py")), "/opt/pika/model_cache.py")
)


@app.function(
    image=image,
    gpu=GPU,
    cpu=12,
    memory=131072,
    volumes={"/mnt/modal": volume},
    min_containers=0,
    max_containers=1,
    scaledown_window=120,
    timeout=7200,
)
@modal.concurrent(max_inputs=1)
@modal.web_server(port=PROXY_PORT, startup_timeout=3600, requires_proxy_auth=False)
def serve():
    """Serve T2VA / FL2VA through one six-step model and HTTP gateway."""
    model_path = get_ready_model()
    cmd = [
        "python", "-m", "sglang.multimodal_gen.runtime.entrypoints.cli.main",
        "serve", "--model-path", model_path,
        "--host", "127.0.0.1", "--port", str(SG_PORT),
        "--performance-mode", "memory",
        "--layerwise-offload-components", "text_encoder,vae",
        "--layerwise-resident-layers", "video_vae=36",
    ]
    print(f"Pika H3 Flex: GPU={GPU}, performance=memory, layerwise-offload=text_encoder,vae, resident=video_vae:36, T2VA+FL2VA", flush=True)
    engine = subprocess.Popen(cmd)
    gateway = subprocess.Popen(["python", "-u", "/opt/pika/proxy.py"])

    def monitor():
        # 两个核心子进程中任意一个退出，都停止另一个，避免静默失效。
        while engine.poll() is None and gateway.poll() is None:
            time.sleep(2)
        print(f"Pika process exited: engine={engine.poll()}, gateway={gateway.poll()}",
              flush=True)
        try:
            for process in (engine, gateway):
                if process.poll() is None:
                    process.terminate()
            volume.commit()
        except Exception as exc:
            print(f"Cleanup failed: {exc}", flush=True)

    threading.Thread(target=monitor, daemon=True).start()
