"""仅使用 CPU 准备 Pika 模型和 SGLang Overlay，并提交共享缓存清单。"""
import json
from pathlib import Path

import modal

from model_cache import CACHE_ROOT, MATERIALIZED_ROOT, MODEL, READY_FILE, VOLUME_ROOT, inspect_snapshot

app = modal.App("modal-forge-009-pika-h3-flex-download")
volume = modal.Volume.from_name("modal-forge", create_if_missing=True)
image = (modal.Image.from_registry("lmsysorg/sglang:dev")
         .entrypoint([])
         # Windows 主机上 str(Path("/mnt/...")) 会变成反斜杠，
         # 导致 Modal 镜像 Dockerfile 解析失败。容器路径必须使用 POSIX 字面量。
         .env({"HF_HOME": "/mnt/modal/009-pika-h3-flex",
               "SGLANG_DIFFUSION_CACHE_ROOT": "/mnt/modal/009-pika-h3-flex/sglang"})
         .env({"PYTHONPATH": "/opt/pika"})
         .add_local_file(str(Path(__file__).with_name("model_cache.py")),
                         "/opt/pika/model_cache.py"))


@app.function(
    image=image,
    # 参考 ComfyUI 下载分片容器的 4 vCPU 配置；SGLang Overlay 物化预留更多内存。
    cpu=4.0,
    memory=8192,
    timeout=86400,
    volumes={VOLUME_ROOT: volume},
)
def download():
    from sglang.multimodal_gen.runtime.utils.hf_diffusers_utils import maybe_download_model

    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    # 不删除旧标记：下载失败时继续保留上一个已验证版本。
    print(f"CPU PREPARE: {MODEL} (overlay + pinned base components)", flush=True)
    # 与推理端使用相同的 SGLang 模型注册与 Overlay 物化逻辑。
    # 强制检查是否真正生成可加载的模型目录，而非只下载 Pika 仓库权重。
    materialized = Path(maybe_download_model(MODEL, force_diffusers_model=True))
    if not materialized.resolve().is_relative_to(MATERIALIZED_ROOT.resolve()):
        raise RuntimeError(f"SGLang materialized model outside persisted Volume: {materialized}")
    details = inspect_snapshot(materialized)
    manifest = {
        "manifest_version": 2,
        "repo_id": MODEL,
        "materialized_path": str(materialized),
        **details,
    }
    temp = READY_FILE.with_suffix(".json.tmp")
    temp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    temp.replace(READY_FILE)
    volume.commit()
    print("CPU DOWNLOAD COMPLETE: " + json.dumps(manifest), flush=True)
    return manifest


@app.function(image=image, cpu=2, memory=8192, timeout=600)
def preflight():
    """只检查 SGLang 模型注册，不下载权重，也不挂载 GPU。"""
    from sglang.multimodal_gen.runtime.utils.hf_diffusers_utils import maybe_download_model
    from sglang.multimodal_gen.runtime.utils.model_overlay import resolve_model_overlay

    overlay = resolve_model_overlay(MODEL)
    if not overlay or not callable(maybe_download_model):
        raise RuntimeError("当前 SGLang 镜像不支持 Pika Overlay")
    print(json.dumps(overlay, indent=2), flush=True)
    return overlay


@app.local_entrypoint()
def main(preflight_only: bool = False):
    result = preflight.remote() if preflight_only else download.remote()
    print(json.dumps(result, indent=2))
