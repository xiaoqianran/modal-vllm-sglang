# 通用 GPU 内存快照

公共 `shared/serving/model_runtime.py` 为所有引擎提供相同生命周期，无模型名称分支。
MOSS、Pika、MiniMax 及新模型模板使用 `"memory_snapshot": "gpu"`；
设为 `"off"` 可回退到普通初始化。旧配置省略此项时仍为 off。
三个模型的 `scaledown_window` 保留 30 秒，`min_containers` 保留 0。

## 生命周期

1. `capture_engine`（拍摄前）：校验缓存，启动引擎子进程，等待引擎健康。
   引擎自身的加载、内置预热和 CUDA 状态纳入快照。
2. `start`（拍摄或恢复后）：重新核对挂载中可见的缓存清单，检查引擎健康；
   新建网关、线程锁、资源监控和子进程监控。快照不包含这些公共运行状态。
3. 关闭快照时，在 `start` 中执行完整引擎初始化，使用同一套服务接口。

GPU 快照同时启用 Modal 的 `enable_memory_snapshot` 与 `enable_gpu_snapshot`。
镜像默认设置 `TORCHINDUCTOR_COMPILE_THREADS=1`，可通过模型 env 显式覆盖。
未使用 LLM 特有的 sleep、KV cache 或 release-memory API；vLLM-Omni 和
SGLang Diffusion 的实际快照兼容性必须分别实测，不能从语言模型示例推定。

## 预热与权重版本

通用层等待引擎健康，不额外生成媒体。Pika 的内置预热会进入快照；
MiniMax 当前 `--warmup-mode off`，以及没有实际请求预热的其他模型，
仍可能在首条请求执行延迟初始化。应同时测首条推理，不能仅检查 health。

Volume 内容变化不会自动让 Modal 重建快照。更新权重应使用新的不可变目录，
并修改配置中的 revision/路径后重新部署。恢复后的清单比对只针对当前挂载
可见的文件，不是对全部权重的实时内容校验，也不能替代版本化。
引擎可能持有 mmap 文件，因此恢复时不强制 `volume.reload()`。

## 验收与计时

缓存检查仍使用完整文件名/大小清单（不是文件内容哈希），不提供降低检查强度的快速模式。
CPU 准备、引擎启动前及快照后的检查分别输出 `cache_verified` 事件，
phase 为 `cpu_prepare`、`engine_start`、`post_snapshot`；包括 seconds 和 success，失败也计时。
普通 off 启动只检查一次；快照路径保留拍摄前和拍摄/恢复后的两次完整检查。
文件遍历对每个普通文件只读取一次 stat，并保持清单排序和原有完整性比较。

必须 `modal deploy <模型目录>/app.py` 后通过部署入口测试；`modal run`
不用于快照验收。首次创建可能更慢，平台也可能为不同 worker 重建快照。
以 Modal 系统日志或 Containers 页面的创建/恢复标记确认实际使用快照。

`engine_initialization_seconds` 是拍摄时的初始化耗时，恢复后保留该历史值。
GPU 快照模式的 `startup_seconds` 仅统计拍摄/恢复后的 start hook，
`startup_measurement=post_snapshot_hook`，不含平台恢复时间，不能当成总冷启动。
off 模式则为 `full_initialization`，包含引擎启动。

每个引擎均应独立对比 off/gpu：相同任务、独立容器，等待 30 秒缩容并确认旧容器退出；
分别记录快照创建、快照恢复、提交到模型就绪、提交到首条产物完成及产物有效性。
至少采集数次恢复，报告中位数与范围。离线测试只能验证生命周期，不能证明
GPU 驱动兼容性、快照恢复成功或实际加速。

参考：[Modal 快照文档](https://modal.com/docs/guide/memory-snapshots)、
[官方 SGLang 示例](https://modal.com/docs/examples/sglang_snapshot)。
