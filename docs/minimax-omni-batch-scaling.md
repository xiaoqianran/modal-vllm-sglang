# MiniMax Omni 双 GPU 扩容实验

## 目标与边界

032/033 的实际日志显示同一批任务的首个容器承担全部 24 次生成，第二个容器初始化完成时已错过有效并行窗口。模型初始化约 125–155 秒。Modal GPU Snapshot 按底层 Worker 类型覆盖，同型号 GPU 不意味着所有 Worker 共享一个快照。官方文档： https://modal.com/docs/guide/memory-snapshots 。本轮增加证据，不尝试强制 GPU 内存跨容器传输，也不改变模型权重和产物协议。

## 实现

- `snapshot_capture_started` / `snapshot_capture_ready` / `model_ready`：关联 capture ID、捕获源及当前 container ID、GPU 型号与 compute capability、驱动、CPU flags 哈希、部署版本和 spec hash。硬件字段不可查询时为空，不影响推理。结合 Modal 平台的 Creating / Restoring 事件判断冷启动或恢复，不能将 3 秒 post-snapshot hook 当成完整恢复时间。环境信息相同也不能证明底层 Worker 类型完全相同。
- MiniMax Omni 默认 `min_containers=0`、`max_containers=2`、`buffer_containers=0`，保留已有输入并发及缩容窗口。取消额外空闲缓冲，并不禁止 Modal 原生自动扩容。
- 可选本地批量控制器：仅对独立、同版本的 4096×2304 / 6 steps T2I 批次启用，沿用批量 admission 和实际生成提交，不创建空转 GPU 请求或额外常驻 CPU 服务。估计单 GPU 每图 12.5 秒，第二 GPU 可用前的额外延迟 180 秒，至少节省 30 秒才主动申请。参数是来自历史日志的保守实验估计，不是账单测量。
- 申请临时 `min_containers=2`，每 5 秒观察剩余工作与控制面输入；出现双容器并行输入、收益不足、无工作、超时或正常停止时恢复初始配置，窗口上限 300 秒。原始设置先写入已有 Modal provisioning Dict，跨进程租约保护，版本或外部设置变化不盲目覆盖。API 无服务器端 TTL；进程硬终止、网络故障或无法证明租约所有者停止可能使临时配置残留，必须人工复核恢复，不能宣称强杀时必然自动缩容。
- 已经启动或归档的任务保持原计划，不迁移、不重新提交。只在新实验中启用主动扩容。

## 下一轮验收（本轮不执行）

1. 运行离线测试：`uv run pytest tests/test_scaling.py tests/test_memory_snapshot.py tests/test_deploy_only.py -q`，再执行全量回归。额外检查普通 worker 仍可提交任务。
2. 准备新部署版本并核对 `uv run python run.py check-scaling minimax-omni`。在同一部署使用不同的新任务 ID，分别测试 8/12 张、24 张与更大批次，保持提示词、分辨率、步数一致。每次记录开始前实际 runner 数、backlog、部署 revision；冷启动测试需确认上一批容器退出。
3. 对照普通 worker 与主动模式：`uv run python run.py worker --task <新实验任务名> --active-scale`。不覆盖或复用 032/033 原任务。观察每个 container ID 的开始、ready、首张、末张、完成数与时间重叠，验证确有实际双 GPU 推理；单有 2 个容器不算通过。记录 Snapshot ID 与平台创建/恢复日志，以及等待、引擎初始化、GPU 推理、发布耗时。至少重复几轮，避免把首次构建快照当作稳态。
4. 对照 GPU 总占用秒数、任务端到端耗时、单图耗时与空闲尾部。短批次不触发主动 override；长批次不强制要求 GPU 供应一定充足。延迟过大应放弃主动申请。正常退出后用 `check-scaling` 核对 min=0、buffer=0；进行 RPC 丢回复与进程终止的恢复测试。
5. 如控制器异常退出，先确认原 worker 已停止，再执行 `uv run python run.py restore-scaling minimax-omni`，随后复查控制面。若租约仍有效、无法证明原所有者结束、配置被其它操作者修改或 API 不可用，应拒绝盲目覆盖，保留恢复凭证供人工核查。实验期间不要并行手工更改 autoscaler 或滚动部署。此模式不默认启用，真实双 GPU、费用及硬终止恢复通过后再决定是否默认开启。

## 说明

主动扩容是请求容量，而不是指定任务去某个容器。实际资源可用性与 Modal 调度决定能否并行。当前 GPU/CUDA 依赖方案不在本轮变更范围内。
