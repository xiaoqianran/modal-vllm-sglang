"""Pika 模型缓存校验：下载端提交清单，推理端只读取完整物化目录。"""
import json
from pathlib import Path

MODEL = "Pika-Labs/Pika-H3-Flex"
VOLUME_ROOT = "/mnt/modal"
CACHE_ROOT = Path(VOLUME_ROOT) / "009-pika-h3-flex"
READY_FILE = CACHE_ROOT / "model-ready.json"
MANIFEST_VERSION = 2
MATERIALIZED_ROOT = CACHE_ROOT / "sglang" / "materialized_models"


def inspect_snapshot(snapshot):
    """检查物化模型、坏链接及文件大小，不重复读取巨型权重内容。"""
    snapshot = Path(snapshot)
    if not snapshot.is_dir():
        raise RuntimeError(f"Materialized model is missing: {snapshot}")
    files = sorted(p for p in snapshot.rglob("*") if p.is_file() or p.is_symlink())
    for item in files:
        if not item.is_file():
            raise RuntimeError(f"Broken or invalid model file: {item}")
    weights = [p for p in files if p.suffix.lower() in (".safetensors", ".bin", ".pt", ".pth")]
    if not weights:
        raise RuntimeError(f"No model weights were found in {snapshot}")
    if not (snapshot / "model_index.json").is_file():
        raise RuntimeError(f"Model index is missing: {snapshot}")
    if not (snapshot / ".sglang_overlay_materialized.json").is_file():
        raise RuntimeError("SGLang overlay materialization marker is missing")
    inventory = {p.relative_to(snapshot).as_posix(): p.stat().st_size for p in files}
    if any(size <= 0 for size in inventory.values()):
        raise RuntimeError("Model snapshot contains empty files")
    total = sum(inventory.values())
    if total == 0:
        raise RuntimeError(f"Model files are empty: {snapshot}")
    return {"file_count": len(files), "weight_count": len(weights),
            "total_bytes": total, "inventory": inventory}


def get_ready_model():
    """GPU 启动前检查 CPU 已提交的文件清单，禁止使用不完整模型。"""
    if not READY_FILE.is_file():
        raise RuntimeError(
            "Pika model was not downloaded on CPU. First run: "
            "uv run modal run download_model.py"
        )
    manifest = json.loads(READY_FILE.read_text(encoding="utf-8"))
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        raise RuntimeError("Model manifest is obsolete; rerun the CPU downloader")
    if manifest.get("repo_id") != MODEL:
        raise RuntimeError("Model cache manifest has the wrong repo_id")
    snapshot = Path(manifest.get("materialized_path", ""))
    if not snapshot.is_absolute() or not snapshot.resolve().is_relative_to(MATERIALIZED_ROOT.resolve()):
        raise RuntimeError("Model cache manifest refers to an invalid materialized directory")
    if snapshot.parent != MATERIALIZED_ROOT:
        raise RuntimeError("Model path must point to an SGLang materialized model")
    actual = inspect_snapshot(snapshot)
    for field in ("file_count", "weight_count", "total_bytes", "inventory"):
        if actual[field] != manifest.get(field):
            raise RuntimeError(f"Model snapshot changed or is incomplete: {field}")
    # SGLang 的 Pika Overlay 使用仓库标识做注册匹配。不能以本地目录代替仓库名。
    return MODEL
