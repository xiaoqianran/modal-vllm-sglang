"""MOSS-Image on Modal via official vLLM-Omni image."""
import os
import subprocess
import threading

import modal

MODEL = os.getenv("MOSS_MODEL", "OpenMOSS-Team/MOSS-Image-SFT")
GPU = os.getenv("MOSS_GPU", "RTX-PRO-6000")
app = modal.App("modal-forge-008-moss-image")
volume = modal.Volume.from_name("modal-forge", create_if_missing=True)
image = (
    modal.Image.from_registry("vllm/vllm-omni:v0.32.0")
    .entrypoint([])
    .env({"HF_HOME": "/root/.cache/huggingface"})
)


@app.function(
    image=image,
    gpu=GPU,
    cpu=8,
    memory=32768,
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
    def persist():
        while True:
            threading.Event().wait(60)
            try:
                volume.commit()
            except Exception as exc:
                print(f"缓存提交失败: {exc}", flush=True)

    threading.Thread(target=persist, daemon=True).start()
    process = subprocess.Popen([
        "vllm", "serve", MODEL, "--omni", "--enforce-eager",
        "--host", "0.0.0.0", "--port", "8000",
    ])
    def monitor():
        print(f"vLLM 退出: {process.wait()}", flush=True)
        volume.commit()
    threading.Thread(target=monitor, daemon=True).start()
