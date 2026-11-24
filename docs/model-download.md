# Modal 多云模型下载与 SGLang 物化

## Pika 下载启动优化（2026-11-22）

当前 Pika 由 `model.json` 的 `cache_implementation` 指向自己的
`models/002-pika-h3-flex/model_impl`，公共下载器不再修改 `sys.path` 指向
某个固定模型目录。专属适配器实现只放在模型内部。

Pika 使用 SGLang 内置注册表的 Overlay 解析与 `materialize_overlay_model`；
项目只负责固定版本文件的预下载、验证和离线输入绑定。**这并不代表
SGLang 不再使用 Overlay 仓库**：上游注册表仍指向固定版本的
`Niehen6174/Pika-H3-Flex-overlay`。

执行顺序现已改为 **轻量 benchmark 下载并验证 → 通过 Debian Slim + uv 构建独立的 SGLang CPU 物化镜像
→ 官方 Overlay 物化 → ready 清单提交**。此前在下载前注册 SGLang 发布函数，
Modal 会过早开始构建完整的大型 SGLang 镜像，导致下载任务长时间没有 Worker。
下载验证结束现在先打印 `DOWNLOAD_VERIFIED`，包含文件数、字节数和耗时。
只在完成完整物化并校验后才会出现 `RESULT`。

CPU 物化镜像定义在 `models/002-pika-h3-flex/model_impl/materializer_image.py`，不再引用
`lmsysorg/sglang` Docker 镜像；它基于 `modal.Image.debian_slim`，用 Modal
原生 `uv_pip_install` 安装项目内已有的固定 SGLang Git 修订版与 Modal 依赖。
**不保证首次构建更快**：SGLang 的 diffusion extras 仍可能安装大型 PyTorch/CUDA
依赖。该固定修订版对 Pika Overlay 注册表的兼容性必须通过远端 CPU 物化实测；
代码保持 fail-closed，不会默默改用其他版本或联网重新下载权重。

GPU 推理仍沿用 `models/002-pika-h3-flex/model.json` 中固定 digest 的 SGLang 运行镜像，
属于单独阶段。仅 CPU 下载和物化已拆分；如果将来需要在 GPU 端也使用
Debian Slim + uv，须先单独验证 CUDA 和 SGLang kernel，不能直接替换生产镜像。

目前仍有一层本地 `snapshot_download` 适配，仅允许上游物化脚本使用
benchmark 已下载且验证的特定 MiniMax-H3 快照，拒绝意外联网补齐；
移除这一兼容层之前，必须验证上游是否提供等价的离线输入接口。
不建议将 GPU `--model-path` 直接替换为远程 HF ID，这会导致 GPU 冷启动时
再次获取大型权重。

`create_downloader` 不提供 `region` 参数，表示不固定区域。调度器会
根据可用后端选择 Worker，历史日志中出现 Azure 并不意味着代码固定 Azure；
实际选择可能跨云及跨区域，不能保证随机、特定价格或某个地理位置。

本项目通过 PyPI 固定依赖 `modal-benchmark==0.4.3`，使用一个 Coordinator + N 个直接 Writer；默认 Xet 可信下载凭证不会因文件时间戳漂移重新扫描完整 SHA-256。
`uv sync` 后使用统一入口 `tools/download_models.py`；旧 `tools/download_azure.py` 作为兼容入口保留。

0.4.2 在明确指定 Volume V2 时默认使用持久 Writer 调度（`scheduling="persistent"`），任务包上限为 10 GiB（`task_batch_bytes=10737418240`，单个超限文件不切分）。Writer 可连续领取任务，减少重复启动和 reload；默认 4 个小文件线程（小于 64 MiB）、跨任务包合并提交（16 文件 / 16 GiB / 45 秒任务边界），Xet 固定并发 8，安全节点补位默认关闭（replacement_budget=0）。Coordinator 使用不可抢占 CPU 容器（该函数 CPU/内存目录价 3 倍）；Writer 仍可抢占。最终仍按持久化凭证验收。现有入口已明确传入 Volume V2，因此无需修改业务调度代码。升级前应停止旧版下载作业；历史缓存与发布规则保持不变。

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
# 免费预览：只读取本地 model.json，不启动 Modal 或访问 Hugging Face
uv run python tools/download_models.py models/002-pika-h3-flex/model.json

# 下载并物化：会产生大量 Modal 云资源及存储费用
uv run python tools/download_models.py models/002-pika-h3-flex/model.json --execute

# 其他模型
uv run python tools/download_models.py models/001-moss-image/model.json --execute
uv run python tools/download_models.py models/005-minimax-h3-image/model.json --execute
```

Worker 配置优先级：`--workers N` > `MODEL_HF_DOWNLOAD_WORKERS` > `model.json` 的 `download_workers` > 默认 3。均支持 1–8 个 Worker。保留现有模型配置（MOSS=3、MiniMax=4、Pika=5、Qwen=2），由 Modal 选择可用云区域。
直接写入各服务的 `/data/<service-cache>/downloads/`，**不兼容或搬迁旧的 `azure/<digest>` 数据**。用户自行清理旧模型目录；若切换固定仓库清单，已有新目录与新清单不一致时应清除相应 `downloads` 目录再执行。
模型下载完成后：MOSS/MiniMax 使用一次远程调用发布 `model-ready.json` 并复核就绪，Pika 单独使用一次 SGLang 容器物化和发布。
下载器和运行时均只使用名为 `model-store` 的单个 Volume V2（`volume_version=2`），挂载在 `/data`；模型位于 `/data/models/<模型目录>`，运行缓存和输出共享同一卷但按目录区分。如果云端原资源仍为 V1，必须先完成云端数据迁移或重新下载；修改 SDK 的 version 参数不会自动转换现有 Volume，旧资源不会自动清理。需要 Modal 凭据及 `huggingface-secret` 的 `HF_TOKEN`。

## Pika 两阶段机制

1. `download_sources()` 为三个仓库生成固定 revision 和按需文件过滤，`modal-benchmark` 只启动实际所需的 Worker，负责 Xet 下载、验证、批量 `Volume.commit()`、失败恢复与独立确认。
2. 物化 CPU 容器执行 `Volume.reload()`，检查下载文件和 Adapter 指定的基础模型 revision，再调用 SGLang `materialize_overlay_model()`。
3. 官方 Pika 物化脚本内部对 `MiniMaxAI/MiniMax-H3` 的 `snapshot_download()` 被限定范围的本地路径适配器接管：请求必须与已下载的固定仓库、revision 和组件过滤完全一致，否则直接报错；不会自动联网重新下载基础权重。
4. 物化成功且文件与 `.sglang_overlay_materialized.json` 检查通过后，才写入现有 `model-ready.json` 并 commit。GPU 启动命令使用 `{model_path}`，不再依赖模型仓库名下载。

Pika 原有 `download_model.py` 仍可独立使用，作为人工恢复路径；自动 `Provisioner` 已统一调用 benchmark。不要让人工准备和自动准备同时写入同一服务缓存（自动流程有分布式 Lease，手工 CLI 没有跨协调器锁）。

## 验证边界

离线模拟测试覆盖三仓库计划、固定版本、拒绝额外联网请求、失败保留旧标记及现有推理路径。
未在此次改动后运行 Pika 273+ GiB 的云端全量下载、真实 SGLang 物化及 GPU 推理；云端是否支持全部文件链接/符号链接和运行镜像中的 SGLang Overlay API 需要专项实测。
Pika GPU 推理默认使用 Modal 原生镜像、固定 SHA-256 的官方 SGLang Nightly wheel 和 CUDA wheel 兼容修复，已通过现有物化权重的真实 GPU 文生视频验收。CPU Overlay 物化器仍使用独立固定的源码版本，重新物化及 GPU Snapshot 恢复需单独验收。详见 [镜像构建策略](image-build-policy.md)。


### Volume 子路径约定

`model-store` 是唯一推理模型 Volume V2。所有推理/物化/维护容器将卷内 `/inference` 使用 Modal `sub_path` 精确挂载为 `/data`。模型缓存位于 `/data/models`，原始 Volume SDK 路径需要补 `/inference` 前缀。当前 modal-benchmark 0.4.3 已支持 `sub_path="/inference"`，Coordinator 和 Writer 均只将 Volume 内该子树挂载为 `/data`；业务方不再负责前缀转换；上传与推理禁止写入 ComfyUI 专用子树。
