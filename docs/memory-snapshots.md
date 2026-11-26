# 通用 GPU 内存快照

媒体服务的公共 `runtime/serving/model_runtime.py` 提供统一快照生命周期，无模型名称分支。
现有 MOSS、Pika、MiniMax 明确配置 `"memory_snapshot": "gpu"`；新模型模板默认 `"off"`，
省略此项也为 off。off 采用按需启动引擎，gpu 采用拍摄前完整引擎初始化。
两种模式不能在同一 GPU 调用中同时实现：需要将已加载的模型纳入快照时，捕获必须在输入到达前完成。
CPU Runner 优先恢复已有产物，因此两种模式都不会主动为已验证的完整产物新派发 GPU。
切换既有模型的配置会改变 spec hash、任务签名和结果前缀，须按新任务计划部署，不改写或重跑既有持久化计划。
Pika 与 MiniMax 的 `scaledown_window` 为 60 秒，MOSS 仍为 30 秒；`min_containers` 保留 0。

## 生命周期

1. `capture_engine`（拍摄前）：校验缓存，启动引擎子进程，等待引擎健康。
   引擎自身的加载、内置预热和 CUDA 状态纳入快照。
2. `start`（拍摄或恢复后）：重新核对挂载中可见的缓存清单，检查引擎健康；
   新建网关、线程锁、资源监控和子进程监控。快照不包含这些公共运行状态。
3. 关闭快照时，`start` 仅校验缓存清单、创建轻量 HTTP 网关和运行状态。SDK 在验证已有
   collecting/completed 产物之后才按需调用 `_ensure_engine`；校验通过的旧产物无需加载引擎。
   合法 HTTP 请求（包含显式健康检查）与新 SDK 推理共用初始化锁；初始化成功前不提交推理。
   `deployment_info` 仍是显式启动引擎的诊断接口。

GPU 快照同时启用 Modal 的 `enable_memory_snapshot` 与 `enable_gpu_snapshot`。
镜像默认设置 `TORCHINDUCTOR_COMPILE_THREADS=1`，可通过模型 env 显式覆盖。
未使用 LLM 特有的 sleep、KV cache 或 release-memory API；vLLM-Omni 和
SGLang Diffusion 的实际快照兼容性必须分别实测，不能从语言模型示例推定。

## 预热与权重版本

Pika 的内置预热会进入快照，显式限制 `--warmup-resolutions 864x480`，不缩减真实请求的画质或六步采样。第三次旧版冷启动实测命中第二份快照，详见 [Pika 性能验收](pika-performance.md)。MiniMax 关闭引擎内置预热，改用可选
`snapshot_warmup.params` 在引擎健康后、快照捕获前执行一次真实的 2048×1152、6 步图像请求。
预热图不发布、不进入任务队列；失败则拒绝就绪。关闭 GPU 快照时此配置不生效。预热和快照恢复是否加速仍须真实验证。
没有配置此项的模型仍可能在首条请求延迟初始化。

Volume 内容变化不会自动让 Modal 重建快照。更新权重应使用新的不可变目录，
并修改配置中的 revision/路径后重新部署。恢复后的清单比对只针对当前挂载
可见的文件，不是对全部权重的实时内容校验，也不能替代版本化。
引擎可能持有 mmap 文件，因此恢复时不强制 `volume.reload()`。

## 验收与计时

缓存检查仍使用完整文件名/大小清单（不是文件内容哈希），不提供降低检查强度的快速模式。
CPU 准备、引擎启动前及快照后的检查分别输出 `cache_verified` 事件，
phase 为 `cpu_prepare`、`runtime_setup`（off）、`engine_start`（gpu）、`post_snapshot`；
包括 seconds 和 success，失败也计时。off 在轻量启动时完整检查一次，后续使用同一已验证的挂载路径；
快照路径保留拍摄前和拍摄/恢复后的两次完整检查。
文件遍历对每个普通文件只读取一次 stat，并保持清单排序和原有完整性比较。

必须 `modal deploy <模型目录>/app.py` 后通过部署入口测试；`modal run`
不用于快照验收。首次创建可能更慢，平台也可能为不同 worker 重建快照。
以 Modal 系统日志或 Containers 页面的创建/恢复标记确认实际使用快照。
同时对比不同容器的 `snapshot_capture_id`，相同捕获 ID 表示继承同一次捕获状态，
新的 ID 表示重新初始化，不能仅凭启动 hook 时间宣称恢复成功。

`engine_initialization_seconds` 是拍摄时的初始化耗时，恢复后保留该历史值。
GPU 快照模式的 `startup_seconds` 仅统计拍摄/恢复后的 start hook，
`startup_measurement=post_snapshot_hook`，不含平台恢复时间，不能当成总冷启动。
off 模式的 `startup_measurement=lazy_runtime_setup` 仅包含运行层和缓存检查。
首次真正需要引擎时独立记录 `lazy_engine_initialization` startup span 及
`engine_initialization_seconds`。不能把轻量启动耗时当作新任务的完整就绪时间。

每个引擎均应独立对比 off/gpu：相同任务、独立容器，等待配置的缩容窗口并确认旧容器退出；
分别记录快照创建、快照恢复、提交到模型就绪、提交到首条产物完成及产物有效性。
至少采集数次恢复，报告中位数与范围。离线测试只能验证生命周期，不能证明
GPU 驱动兼容性、快照恢复成功或实际加速。

参考：[Modal 快照文档](https://modal.com/docs/guide/memory-snapshots)、
[官方 SGLang 示例](https://modal.com/docs/examples/sglang_snapshot)。

## MOSS 快照 worker 覆盖复测（2026-11-26）

同一生产部署 revision 2、固定 RTX PRO 6000，前三个独立容器的 Modal 系统日志均为
`Creating GPU memory snapshot for Function.`，Capture ID 各不相同。
第四个独立冷启动（确认容器数为 0）命中历史捕获 `fd5b971c3c2849b9b8b3657876765be7`，
平台明确记录 `Restoring Function from memory snapshot.`。后置 hook 3.46 s，
SDK 端到端 174.69 s（含平台调度等等待），引擎初始化 60.96 s 为历史快照值，并未重新执行。
官方说明 GPU Function 通常需要每种 GPU 2–3 份快照以覆盖不同 worker 类型；
本轮现象与该机制一致，但未获取各 worker 的内部类型，不能逐一确定重拍原因。
既有 MOSS/Pika/MiniMax 配置不因本轮实验自动切换；每次修改代码并重新部署都可能重新捕获快照。

## MOSS 按需初始化真实 GPU 验收（2026-11-26）

独立实验 App 直接使用生产 ModelRuntime 与 generate_one，固定 RTX PRO 6000，
关闭 Snapshot。第一次尝试因 Modal 等待分配超过 300 秒而取消，重试成功。
同一容器依次执行 collecting 恢复、completed 复用、新图像生成与再次复用。
nvidia-smi 确认 NVIDIA RTX PRO 6000 Blackwell Server Edition。

| 操作 | 端到端 SDK 耗时 | 容器内执行 |
| --- | ---: | ---: |
| collecting 恢复 | 8.46 s | 2.00 s |
| completed 复用 | 0.83 s | 4.73 ms |
| 1024×1024、4 steps 新图像 | 74.04 s | 73.58 s |
| 新图像重复读取 | 0.80 s | 5.61 ms |

轻量运行层启动 1.51 s（含完整缓存校验 1.48 s），前两次调用 engine_ready=false，
GPU 显存采样 7 MiB。仅第三次启动引擎，初始化 69.04 s，实际扩散推理 1.60 s，
内部 HTTP 网关返回 200；新图像和恢复产物均 3,147,861 字节且逐一校验 SHA-256。
第四次命中同一产物，不重新执行推理。独立实验报告与原始日志保存在 PC2
的 .runtime/999-lab-lazy-cf7fc3380ff3/report.json 和 .runtime/lazy_moss_retry_1126.log。
该实验通过 SDK 与内部 HTTP 网关，不涉及外部 Proxy Auth 凭据验证。

## CPU 恢复 SDK 请求对照（2026-11-26）

使用前一轮 PRO 6000 硬退出实验的真实 collecting 凭证和 3,147,861 字节产物，
在四个独立 Volume 路径中复现同一终态 Call 的恢复逻辑。原算法成功写入不可变
凭证后重复读取原始记录与凭证；新算法仅在发布冲突时重新读取验证。

| 指标 | 原路径 | 优化路径 |
| --- | ---: | ---: |
| Volume.read_file 调用次数 | 5 | 3 |
| 终态查询 | 1 | 1 |
| 不可变凭证发布 | 1 | 1 |
| 本地 SDK 实测（两次） | 7.73 / 6.04 s | 4.02 / 3.71 s |

两组产物 SHA-256 一致，全部成功恢复。实测为本地 Runner 到 Modal 的小样本交叉对照，
不是云端 CPU 容器内延迟或长期吞吐结论。仅统计 SDK 方法调用次数，不等同底层 gRPC 请求数量。
实验专用目录已删除并验证不存在，证据保存在 PC2 的 .runtime/rpc_recovery_ab_1126.json。

## 独立图像批量提交

正式任务入口仍使用 `python run.py run tasks/<任务>.json`，由云端调度依赖和独立收集池。
对于仅需本地图片的独立直连实验，可在部署版本确认后执行：

```bash
uv run --no-sync python -m tools.run_image_batch minimax tasks/021-minimax-cinematic-atlas-12.json outputs/batch-001 --project batch-001
```

在收集任何图片之前先登记所有独立请求，GPU 仍单容器串行执行；本地下载、校验使用独立线程池。
使用唯一项目名和目录，保存提交回执与结果哈希。已有目录的提交标记会拒绝重复运行，
不确定的提交不得盲目重试。此工具不负责部署或 HF 发布。
MiniMax 60 秒缩容仅为额外缓冲，不代替排队。预热将首次编译成本移入快照创建，
首次创建可能更慢；只有实际恢复成功且结果正常才能证明优化有效。

## Qwen3.8-27B + Pi LoRA 冷启动实测（2026-11-21）

在独立 Modal 测试端点使用单张 RTX PRO 6000、vLLM 0.34.0、未量化 BF16 基座和同一 Pi LoRA。
客户端就绪时间从发出请求算到 `/v1/models` 返回。热态生成速度取同一提示模板下两次热请求的中位数，
每次输出 256 token，首 token 等待单独计时。130K 长上下文是独立的功能测试。

| 测试方案 | 客户端等到模型可用 | 热态生成速度 |
| --- | ---: | ---: |
| 对照组 | 223.7 秒 | 22.8 token/s |
| 关闭 FlashInfer 自动调优 | 212.4 秒 | 22.8 token/s |
| Fast Boot（关闭调优并启用 `--enforce-eager`） | 203.5 秒 | 22.9 token/s |
| GPU Snapshot 恢复 | 29.8 秒 | 23.2 token/s |

快照组首次创建并就绪约 203 秒。Sleep Mode level 1 将约 50.7 GiB 权重转存到 CPU，测试容器配置了 96 GiB 主存。
原容器缩到零后，新容器的 Modal 日志出现 `Restoring Function from memory snapshot`，并唤醒同一个捕获 ID。
vLLM 唤醒步骤约 1.4 秒，29.8 秒是客户端端到端时间。恢复后，LoRA 流式聊天、130,034 输入 token 的请求，
以及 Pi 读取测试文件并把工具结果交回模型均通过。

以上是单轮对照，并行读取权重存在波动。Fast Boot 本轮就绪快约 20 秒，但首次流式请求的首 token 等待从约 0.9 秒增至约 3.0 秒，暂不设为默认。
GPU Snapshot 的收益主要体现在首次可用时间，几个方案的热态单请求生成速度接近。

临时测试 App 已停止，正式端点仍以 `memory_snapshot: off` 运行。后续可在独立的
[`llm_runtime.py`](../runtime/serving/llm_runtime.py) 里加入可选的 Snapshot + Sleep Mode 生命周期，
多次缩到零后复测恢复时间、LoRA、SSE、Pi 工具调用及长上下文，再考虑启用。
参考：[Modal 官方 vLLM 快照实现](https://github.com/modal-labs/modal-examples/blob/main/06_gpu_and_ml/llm-serving/ministral3_inference.py)。
