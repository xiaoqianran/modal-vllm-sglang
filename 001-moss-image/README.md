# MOSS Image

本模型使用公共模板，模型能力为 moss-t2i。使用 vLLM-Omni，默认 1024×1024、50 步。

- model.json：模型、镜像、启动命令、权重组件、资源、参数与能力配置。
- app.py / download_model.py / run.py：公共实现的薄入口。
- 公共实现：shared/model_service.py、shared/model_cache.py、shared/model_spec.py。
- 日常任务在根目录 tasks/，素材在 inputs/，结果在 outputs/<project>/<scene>/。

从仓库根目录执行：

```powershell
uv run python 001-moss-image/run.py --task tasks/003-stop-rain-001-rain-bus-stop.json --dry-run
uv run python -m modal run 001-moss-image/download_model.py
uv run python -m modal deploy 001-moss-image/app.py
uv run python 001-moss-image/run.py --task tasks/003-stop-rain-001-rain-bus-stop.json
```

下载器 --check-only 检查 CPU 缓存。模型保留部署名称 Moss Class，SDK 与私有 HTTP 共用引擎。
完整任务格式、恢复机制与验证说明见 [根目录说明](../README.md)。
本轮代码迁移没有触发云端部署或付费 GPU 验收。

## 异步任务（MOSS 首版）

更新代码后需要重新部署 `app.py`，云端 Class 才有 `generate_one` 方法。
准备过且未变化的权重不需要重新下载。以下提交命令会触发实际 GPU 生成：

```powershell
# 校验任务，不连接 Modal
uv run python 001-moss-image/run.py --task tasks/003-stop-rain-001-rain-bus-stop.json --enqueue --dry-run
# 每个 item 独立 spawn；返回调用 ID 后即可关闭客户端，随时可提交下一份 JSON
uv run python 001-moss-image/run.py --task tasks/003-stop-rain-001-rain-bus-stop.json --enqueue
# 查询本地登记的所有 MOSS 调用，不启动 GPU
uv run python 001-moss-image/run.py --status
# 只下载已经完成的结果，不等待其余调用，不启动 GPU
uv run python 001-moss-image/run.py --collect
# 不依赖本地 moss.json，直接根据任务 JSON 从云端 Volume 收集对应结果
uv run --no-sync python 001-moss-image/run.py --collect --remote --task tasks/004-seven-minutes-001-glass-corridor.json
```

`--status`、`--collect` 可以加 `--task <路径>` 筛选；不加时读取整个登记表。
新模式 `--collect --remote` 必须提供至少一个 `--task`，支持多次传入 `--task`。
它仅通过 Task 内容、当前模型配置与云端模型缓存生成确定性路径和签名，
检查 Volume 中已有的逐项完成元数据，验证任务意图、模型缓存和 SHA-256 后下载媒体。
下载采用本地 `ThreadPoolExecutor(max_workers=8)` 八路并发，默认同时处理至多八张图片；
普通 `--collect` 也启用八路下载，同一镜头独立文件锁保证不重复覆盖。
不同镜头可并行查询及下载，某张失败不取消其他已开始的下载；
失败会在其他任务处理完后报告，下一次可安全重试收集。不会启动 GPU。
不读取或创建本地 `outputs/.runner/jobs/moss.json`，不需要 Modal FunctionCall ID，
也不会提交新的生成任务或启动 GPU。只收集明确标记 `completed` 的项目；
云端不存在元数据时显示 `not_found`，不代表任务一定没有排队或执行。
如果任务内容、模型配置或缓存已经变化，必须使用生成时的原始 Task 和配置，
不能通过忽略签名或强制覆盖来下载可能不匹配的媒体。
这一模式不自动扫描历史云端目录，也不用于补回丢失的提交登记；
远程任务的提交、状态恢复仍建议保留原本的 `moss.json`。
可重复使用 `--task` 一次提交多份 JSON；同一场景的不同 ID 可以分别提交。
原有不带 `--enqueue` 的同步生成命令继续可用。

调用 ID 与任务身份保存在 `outputs/.runner/jobs/moss.json`，媒体仍位于
`outputs/<project>/<scene>/`。使用 `--output` 时，这两个位置一起迁移；后续提交、
查询和收集必须使用同一个目录。保留该登记表，提交响应丢失或程序退出时才能保守恢复。
不同电脑或不同输出目录不共享本地提交锁，不能将其作为跨客户端的严格 exactly-once 保证。

状态说明：`queued` 表示已接受且尚无可确认的执行结果（不保证精确区分排队和启动）；
`running` 表示 Volume 已存在执行记录；`completed` 表示取得匹配的完成元数据；
`failed` 表示远端元数据确认失败；`uncertain` 表示提交响应丢失、调用过期、异常退出
或查询失败等情况。`completed` 的文件在 `--collect` 时校验 SHA-256。
状态查询优先使用 Volume，因此调用返回值过期后仍可恢复已提交的完成结果。
失败或未知状态都不会自动重发；可稍后再次查询，或核查 Modal 日志及 Volume 元数据。
不要通过删除登记表来强行重试未知任务。参数、配置或缓存变化但输出身份相同时会报冲突。

MOSS 保持 `min_containers=0`、`max_containers=1` 和 30 秒空闲窗口，容器输入并发为 1。
这让长任务在 Modal 队列等待，避免 8 个输入在容器内等待生成锁；旧版流式方法也共享
这个限制。HTTP 入口保留，但请求可能等待 SDK 生成完成；尚未做线上共存与超时验收。
Pika、MiniMax 保留原有并发设置，暂不开放这些异步 CLI 选项。
没有新增 GPU 服务、常驻 CPU 调度器、Queue、Dict 或外部队列。

日志包含 `model_ready`、`generate_one`、`generate_one_completed`，可按容器任务 ID、
输入 ID 和输出路径核对启动次数与连续生成情况。连续排队通常可以复用热容器，
但不保证 FIFO、固定任务间隔、容器永不回收或只加载一次模型。
真实 GPU 验收仍需进行：3 项独立提交、20 项连续排队、提交后关闭客户端并恢复收集、
重复提交与异常退出核查，再根据日志和账单评估热容器复用与费用。
