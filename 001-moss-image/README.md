# MOSS Image — Deploy 后直接生图

与 Pika、MiniMax 一致：**一个 `Moss` GPU Class，两个可选入口，一份 vLLM-Omni 模型进程。**

## 核心文件

| 文件 | 作用 |
| --- | --- |
| `app.py` | 唯一 `Moss` GPU Class、模型启动与私有 HTTP、SDK 入口 |
| `image.py` | 固定参数、逐张生成、Volume 哈希校验和恢复 |
| `run.py` | 本地指定 JSON、下载图片、断点续跑、OS 写入锁 |
| `model_cache.py` / `download_model.py` | 缓存完整性检查和 CPU 准备 |
| `download_storyboard.py` | **只读历史结果恢复**，不启动 GPU |

旧的两个批量入口 `scripts/run_moss_batch.py`、`scripts/generate_again_love_v3.py` 已合并为 `run.py`；独立 GPU Benchmark 和旧 SGLang 下载脚本已删除。历史 GPU 实验数据另存为 [2026-11-08 benchmarks](docs/2026-11-08-benchmarks.md)。

## 部署一次

在 `001-moss-image` 目录：

```powershell
uv run python -m modal run download_model.py --check-only
# 未下载权重时才需：uv run python -m modal run download_model.py
uv run python -m modal deploy app.py
```

- Modal App：`modal-forge-008-moss-image`
- 模型：`OpenMOSS-Team/MOSS-Image-SFT`（可通过 `MOSS_MODEL` 指定）
- GPU：**RTX PRO 6000**，4 CPU / 8 GiB RAM
- 镜像：`vllm/vllm-omni:v0.32.0`，固定镜像启动参数 `--omni --enforce-eager`
- `min_containers=0`、Class 级 `max_containers=1`、空闲缩容 2 秒
- 模型文件存于既有 Modal Volume `modal-forge` 的 `007-moss-image` 缓存路径；禁止 GPU 上联网补权重。

## 本地只指定 JSON

从仓库根目录：

```powershell
uv run python 001-moss-image/run.py `
  --task tasks/003-stop-rain-001-rain-bus-stop.json `
  --task tasks/003-stop-rain-002-bridge-underpass.json `
  --task tasks/003-stop-rain-003-glass-greenhouse.json `
  --task tasks/003-stop-rain-004-after-rain.json
```

加 `--dry-run` 仅验证并预览，不申请 GPU。必须显式指定至少一个 `--task`；项目名从首个 JSON 自动读取，仍可用 `--project` 手动指定。不再默认执行其它剧本。

本地 SDK 通过 `modal.Cls.from_name("modal-forge-008-moss-image", "Moss")()` 自动定位部署，不需要 `MOSS_ENDPOINT` 或 Proxy Token。图片写入 `outputs/<project>/<scene>/` 并校验 SHA256；只补缺失图片。历史未下载的远端结果可使用 `--recover-prefix` 手动恢复。

## HTTP 与注意事项

`serve` 继续提供 `POST /v1/images/generations`，并启用 `requires_proxy_auth=True`，供外部服务及跨模型 HTTP Workflow 使用。HTTP 和 SDK 共用同一个 vLLM 实例，**不因两个入口而启动第二张 GPU**。

单容器并不等于 GPU Snapshot。MOSS 尚未接入显存快照；Class 合并代码还需真实云端测试。**旧的批量推理尚在运行时不可重新 Deploy，必须先让结果保存完整。**

历史性能、冷启动与 L40S 比较见 [实测记录](docs/2026-11-08-benchmarks.md)，详细续跑协议见 [MOSS resume](../docs/moss-resume.md)。
