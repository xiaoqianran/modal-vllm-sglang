# Modal 多云模型下载与 SGLang 物化

本项目通过 PyPI 发布的 `modal-benchmark==0.1.5` 复用直接下载调度，不复制调度器源码，也不依赖本地相邻仓库。
`uv sync` 后使用统一入口 `scripts/download_models.py`；旧 `scripts/download_azure.py` 作为兼容入口保留。

## 三种模型

| 模型 | Modal 下载 | 后处理 |
|---|---|---|
| MOSS Image | modal-benchmark | 原有模型缓存发布 |
| MiniMax H3 Image | modal-benchmark（模型 + 编码器） | 原有模型缓存发布 |
| Pika H3 Flex | modal-benchmark（三个仓库） | SGLang 原生 Overlay 物化 |

Pika 的三个固定版本输入为：
- `Pika-Labs/Pika-H3-Flex@3ff166447d5edb1406107ab8370c42000ce173e4`：仅两个 Adapter 和清单
- `Niehen6174/Pika-H3-Flex-overlay@2d8d5e4d81d2ad0ba4b0bbcf7049231bfbe3e9c2`：Overlay 元数据和物化脚本
- `MiniMaxAI/MiniMax-H3@42ed227ee7df40d41602854ae760620d6eb651fe`：FL2VA/Ref2VA 所需基础组件

2026-11-16 的 HF 元数据清单：7 个 Pika 文件约 5.154 GiB，4 个 Overlay 元数据文件，160 个 MiniMax-H3 基础文件约 268.316 GiB。首次完整准备可能需要约 **273.5 GiB**（不含缓存及物化额外存储）；不要把它当成 5.5 GB 模型下载。现有 Pika 的 t2va/fl2va 主要使用 FL2VA，但为保持官方物化语义仍包含 Ref2VA。

## 执行

```powershell
uv sync
# 预览：远端清单发现可能产生少量云费用，不下载模型文件
uv run python scripts/download_models.py 002-pika-h3-flex/model.json

# 下载并物化：会产生大量 Modal 云资源及存储费用
uv run python scripts/download_models.py 002-pika-h3-flex/model.json --execute

# 其他模型
uv run python scripts/download_models.py 001-moss-image/model.json --execute
uv run python scripts/download_models.py 005-minimax-h3-image/model.json --execute
```

默认直接启动 2 个 Worker，不申请候选容器（显式传参最多 5 个）。不按 Azure、GCP、AWS 过滤，也不强制 region。文件按传输成本与逐文件固定开销分配给 Worker；每台 Worker 使用本地任务与重试队列，不再有远程共享下载队列。`--workers auto` 可按文件清单估算 Worker 数。
为兼容既有缓存与断点恢复，Volume 中下载路径暂时仍保留 `/azure/` 历史目录名；它不代表 Azure-only 策略。
Worker 数决定下载并发，SGLang 后处理单独在 CPU 容器中执行，不按 Worker 数复制。
新入口使用既有 `modal-vllm-sglang` Volume（`volume_version=None`），不会自动从 v1 升级到 v2。需要 Modal 凭据及 `huggingface-secret` 的 `HF_TOKEN`。

## Pika 两阶段机制

1. `download_sources()` 为三个仓库生成固定 revision 和按需文件过滤，`modal-benchmark` 只启动实际所需的 Worker，负责 Xet 下载、验证、批量 `Volume.commit()`、失败恢复与独立确认。
2. 物化 CPU 容器执行 `Volume.reload()`，检查下载文件和 Adapter 指定的基础模型 revision，再调用 SGLang `materialize_overlay_model()`。
3. 官方 Pika 物化脚本内部对 `MiniMaxAI/MiniMax-H3` 的 `snapshot_download()` 被限定范围的本地路径适配器接管：请求必须与已下载的固定仓库、revision 和组件过滤完全一致，否则直接报错；不会自动联网重新下载基础权重。
4. 物化成功且文件与 `.sglang_overlay_materialized.json` 检查通过后，才写入现有 `model-ready.json` 并 commit。GPU 启动命令使用 `{model_path}`，不再依赖模型仓库名下载。

Pika 原有 `download_model.py` 仍可独立使用，作为人工恢复路径；自动 `Provisioner` 已统一调用 benchmark。不要让人工准备和自动准备同时写入同一服务缓存（自动流程有分布式 Lease，手工 CLI 没有跨协调器锁）。

## 验证边界

离线模拟测试覆盖三仓库计划、固定版本、拒绝额外联网请求、失败保留旧标记及现有推理路径。
未在此次改动后运行 Pika 273+ GiB 的云端全量下载、真实 SGLang 物化及 GPU 推理；云端是否支持全部文件链接/符号链接和运行镜像中的 SGLang Overlay API 需要专项实测。
`lmsysorg/sglang:dev` 是可变镜像标签：后续生产部署应固定经过真实验证的镜像 digest。
