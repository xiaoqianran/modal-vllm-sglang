# telemetry 运行记录

telemetry 只观察执行，不控制生成和恢复。Task 仍通过 orchestration run 登记，worker 负责执行；
state.json 与云端结果元数据仍是恢复权威。事件丢失、采集失败或报告失败都不是重新生成的理由。

## 固定名称

| stage | 含义 |
| --- | --- |
| register | 校验并登记工作流计划 |
| submit | 本地向 Modal 提交并获得调用 ID |
| wait | 等待，reason 固定为 dependency / lock / platform |
| startup | 本次云端容器启动钩子 |
| generate | 图片请求，或视频 submit / wait / fetch；细分放 attributes.operation |
| save | 保存云端产物及完成元数据，包含对应 Volume commit |
| transfer | 本地输入图片上传，或 Pika 云端读取、校验首尾帧 |
| download | 下载、校验和发布本地文件，已有文件的校验也记录 reused |

阶段状态只用 running / completed / failed。
业务关联字段只用 task_id（project/scene）、job_key、call_id、container_id、engine_job_id。
event_id / span_id / producer_id 是事件去重、起止配对和文件生产者标识，不是另外一套任务身份。
云端最初不知道 Modal call_id，通过 task_id + job_key 关联本地登记结果；报告标记这类补充关联。

## 模块

```text
runtime/telemetry/
  schema.py      字段、阶段、状态、等待原因与校验
  recorder.py    span、interval、resource，接入业务边界
  sampler.py     CPU / 内存 / 显存采样
  storage.py     有界缓冲、后台写入、不可变分片和清单
  collector.py   云端收集、哈希校验、去重和 SQLite 索引
  report.py      Task 时间线、阶段汇总、采样峰值和覆盖缺口
```

业务代码只声明阶段：

```python
with recorder.span("transfer", operation="read_frames"):
    frames = read_cloud_frames(...)
```

阶段默认 measurement=measured，使用本进程高精度单调时钟计时。
持久化依赖等待的起点在其他进程中恢复时，用 UTC 差值，标为 derived。
UTC 用于展示跨机器时间线，不冒充精确的跨机器耗时。
失败事件只保存异常类型，不保存可能含凭据或输入内容的异常正文。
请求只携带 task_id / job_key 关联信息；这些信息不进入推理请求参数和请求签名。

## 落盘与收集

```text
Modal Volume: modal-vllm-sglang
  telemetry/<producer_id>/
    00000000.jsonl                 不可变事件与资源样本分片
    manifest-<编号>.json           每份结果独立的收集清单
    status.json                   写入诊断

outputs/.telemetry/
  raw/local/<producer_id>/         本地原始记录
  raw/cloud/<producer_id>/         收集并校验的云端原始记录
  raw/manifests/                   已收集的结果清单
  index.sqlite                    可重建的查询索引
  collector.json                  后台收集诊断
  reports/<task_id摘要>.json       每 Task 报告
```

每个生产者使用独立目录，不竞争同一个共享日志文件。
JSONL 分片写入后不修改；清单记录路径、字节数和 SHA256。索引以 event_id 去重，
重复收集不重复计数；同一事件 ID 对应不同内容、同一分片内容被修改时拒绝接纳。

云端完成结果携带 telemetry 清单引用，worker 将收集请求交给独立后台线程。
先允许图片或视频产物完成、解锁下游，再独立收集记录，不让日志下载挡住视频生成。
如果 worker 先观察到产物完成、清单尚未提交，后台按结果中预先保存的清单引用继续查询。
每个 job 的清单独立，避免复用容器时误读上一份任务的旧清单。

本地和云端事件写入均使用有界后台队列。资源探测在采样线程执行，阶段边界只发出采样请求，
不会等待 nvidia-smi。云端清单发布需要本地缓冲刷盘及 Volume commit，采用尽力而为策略；
失败只写 telemetry_error，不改变已经成功的产物状态。
原始分片保留证据，索引和报告是可重建的派生数据。

## 使用

正常 run / worker 自动接入，无需改变 Task JSON。
云端代码变更后必须重新部署涉及的 app.py；本地代码更新不会改变已运行的容器。

```powershell
# 检查已部署的云端接口、存储及 telemetry 版本；会启动 GPU，不生成媒体
uv run python run.py check-deployment moss pika

# 只整理本地记录，不访问云端、不启动 GPU
uv run python run.py telemetry collect --local

# 从已有任务的结果引用补收云端记录，不提交生成、不启动 GPU
uv run python run.py telemetry collect

# 读取本地证据并生成、输出 Task 报告，不访问云端
uv run python run.py telemetry report outputs/006-moss-pika-demo/001-main
```

后台收集以结果清单为入口，不扫描整个 Volume。正常情况下，某项工作完成后会收集其记录、
更新报告；这不是一个在推理过程中持续拉取云端曲线的实时监控页面。
异常任务如果未能留下可发现的清单引用，云端记录可能无法自动关联，应结合 Modal 日志排查。

## 资源与计时口径

- telemetry_interval 在 model.json 配置，默认 5 秒、正整数；旧 metrics_interval 字段不再接受。
- CPU / RAM 读取容器 cgroup v2，包含引擎子进程；GPU 读取 nvidia-smi。
- 阶段资源只关联同一生产者、单调时钟范围内的样本。sampled_ram_peak_bytes 使用当前内存采样，
  不把容器历史 ram_peak_bytes 当成单个阶段峰值；CPU 增量只覆盖所选样本之间的区间。
- 短阶段可能没有足够样本，指标保留 null；采样可能漏掉短暂尖峰。数据不是任务独占资源计费值。
- 普通初始化时采样器在启动引擎前开始。GPU 快照模式不在拍摄前启动记录线程；恢复后创建新生产者，
  startup 只表示 post_snapshot_hook，初始化耗时标为快照历史值，不算作本次恢复的重新初始化。
- 当前没有可靠平台证据拆分 Modal 排队与完整快照恢复，platform_wait_ms / snapshot_restore_ms 固定为 null。
  不把提交到观察完成的总间隔伪装成排队时间。
- 各阶段可能跨任务重叠；stage_totals_ms 不能相加当成整体墙钟时间。observed_elapsed_ms 是观测时间跨度。
- 报告列出 missing_stages、各 job 的覆盖缺口、unclosed_spans 和生产者丢弃/错误计数。
  尚未发生、旧版本未采集或异常退出丢失的阶段都可能形成缺口，不能把缺失当作零耗时。
- 突然终止进程可能丢失尚未刷盘的记录。状态文件仍可恢复业务；telemetry 不承诺远端恰好一次记录。

## 验证

离线测试模拟整条图片到视频再到下载的链路，验证阶段关联、资源区间、重复收集、清单发布竞争、
失败隔离、慢写入、未闭合阶段和下载统计。不会连接云端或调用真实模型。
真实 GPU 资源值、快照恢复和平台侧时序仍需部署后验收。
