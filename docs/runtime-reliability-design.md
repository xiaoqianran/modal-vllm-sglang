# 运行时可靠性设计：版本专属探针与可恢复传输

日期：2026-11-25。状态：已实现，PC2 回归及 CNB 线上非 GPU 验收通过。
适用提交：[b849892](https://github.com/xiaoqianran/modal-vllm-sglang/commit/b849892)、[2914e36](https://github.com/xiaoqianran/modal-vllm-sglang/commit/2914e36)。

## 1. 核心设计原则

**不要等待旧版本消失，而要直接验证新版本存在。** 控制面的就绪检查与 GPU 数据面的推理分离；
任务执行与产物传输分离；任何恢复操作都应复用已有的可信结果，而不是重放昂贵的计算。
版本号、缓存指纹、调用回执和内容摘要分别描述不同身份，不能互相替代。

## 2. 滚动部署：内容寻址探针

### 故障机制与触发条件

滚动部署允许旧容器暂时存活。此前所有版本共用 `provision_info`，
每隔约 3 秒重新查询一次，可能持续访问旧容器。2026-11-25 的 MiniMax 故障中，
v3 已发布但探针仍返回 v2 的哈希；优雅停止旧探针容器后才恢复。
该现象可在源码、配置或模型缓存变化触发滚动部署，以及旧探针尚未退出时出现。
反复查询延长旧容器存活是合理的机制解释，不代表已证明每次旧容器滞留都由此造成。
历史记录中返回 `manual` 的另一次失败，不应简单归为同一根因。

另一个独立缺陷是 Windows Git checkout 使用 CRLF、Linux 使用 LF，
旧算法对原始文件字节哈希会让相同 Git 提交产生不同源码指纹。
因此对 Python/JSON 统一 CRLF 为 LF，按 POSIX 相对路径排序后计算指纹，
不忽略其他内容变化。Git HEAD 相同不等于工作区相同，更不等于模型缓存相同。

### 最终方案

```mermaid
flowchart TD
    A["源码 + 配置 + 已验证缓存"] --> B["计算预期部署哈希"]
    B --> C["查询当前 Modal App 清单"]
    C --> D{"存在哈希专属探针？"}
    D -- 否 --> E["尚未就绪；按需部署或等待"]
    D -- 是 --> F["调用专属轻量 CPU 探针"]
    F --> G["比较完整哈希并再次检查 revision / 函数 ID"]
    G --> H{"一致？"}
    H -- 否 --> E
    H -- 是 --> I["持久化 Ready 与 revision"]
    I --> J["按已验证身份提交任务"]
    J --> K["执行入口再次核对完整哈希"]
```

- 每个托管部署注册 `provision_info_<hash前32位>`，避免调用旧版本同名探针。返回并比较的是完整 64 位十六进制哈希，不仅仅是名称中的前缀。固定名称仅作手动/旧版诊断，不能作为托管版本通过的证据。
- 先读取当前 App 清单；没有目标 tag 时不启动 CPU 容器。调用前后核对当前 App revision 和 tag 对应的函数 ID，防止滚动发布或回滚竞态。
- 探针使用 `single_use_containers=True`、`scaledown_window=2`、CPU 0.25 核、512 MiB 内存、执行超时 15 秒、启动超时 30 秒、平台重试 0；本地调用等待上限 35 秒，超时取消该探针调用，不强制关闭 GPU 容器。
- CNB 的 chutianqiuer 工作区未配置历史 revision 保留，实测 `Function.from_name(..., version=revision)` 被 Modal 拒绝。因此最终方案仅查询当前 App 的专属函数，不要求付费套餐或历史版本调用。
- CPU 编排使用 `run_shot_<hash前32位>` 专属入口；GPU 保留稳定 Class 名，句柄按部署哈希隔离，新托管请求携带完整预期哈希，执行入口在生成前拒绝不匹配版本。旧容器若收到新请求，可能拒绝请求而不是执行错误版本；这不是保证所有旧 GPU 容器自动消失的机制。
- 继续使用 rolling；共享 admission/lease 保护在途任务，已接受调用按原 call ID 查询。禁止因校验不确定而盲目重部署、重放生成或强制重建 App。显式诊断与实际 GPU 验收仍是不同阶段。

### 实测结果

| 服务 | 最终 Modal revision | 部署校验耗时 | 结果 |
| --- | --- | ---: | --- |
| MOSS | 5 | 4.73 s | Ready |
| MiniMax | 5 | 6.59 s | Ready |
| Orchestration | 2 | 5.23 s | Ready |

历史 MiniMax 校验 162.26 s（含人工停止旧探针的等待），最终 6.59 s，
单次观测下降约 95.9%，不是同条件多轮 benchmark，也不是模型加载或整次部署速度。
最终 MiniMax 构建部署阶段另耗时约 16.21 s。三个 App 的期望/实际哈希完全相同，
单次探针配置线上确认，结束后容器列表为空；没有人工停止旧容器或重新下载权重。
PC2 与 CNB 的四模型和编排共五个源码指纹逐项一致。

## 3. 大文件：分块验证、恢复与单遍完整哈希

HF 模型下载沿用 `verification_mode="xet"`（参见 `tools/download_models.py`）；
生成结果从 Modal Volume V2 下载不是 HF Xet，使用 Volume 原生分块传输和数据块摘要。

```mermaid
flowchart TD
    A["已提交的云端产物 + 期望 SHA-256 / 大小"] --> B["获取 Volume V2 块清单"]
    B --> C["验证本地断点清单与已保存块"]
    C --> D["复用正确块，下载缺失或损坏块"]
    D --> E{"块完整性通过？"}
    E -- 否 --> F["刷新失败块链接，有界重试"]
    F --> D
    E -- 是 --> G["持久化块及摘要；按文件顺序更新完整哈希"]
    G --> H{"完整哈希匹配？"}
    H -- 否 --> I["拒绝发布，清除无效断点"]
    H -- 是 --> J["原子发布成品"]
```

大文件至少 8 MiB 且提供期望大小和 SHA-256 时启用 V2 分块恢复。
当前每块 8 MiB，最多 3 块并行，每次总超时 60 秒、读空闲超时 20 秒、
最多 3 次尝试；失败块单独刷新下载链接。分组等待确保已成功兄弟块不会因另一块失败丢失，
但组内仍有慢块等待，不能宣称彻底消除网络停顿。

临时文件 `<name>.volume-part` 与 `<name>.volume-part.json` 保存恢复状态。
恢复身份包含 Volume ID、路径、期望 SHA-256、长度和块布局；已保存块重新验证后才复用。
首次下载直接对内存中的有序数据计算完整 SHA-256，避免完成后再读取整个文件。
续传时重新读取已保留块重建哈希，新的块直接计算；不信任序列化哈希状态或未校验的稀疏文件。
完整摘要和媒体头检查通过才原子替换成品。小文件、V1 或不支持该适配器的后端使用普通流式下载。

### 真实文件恢复验收

使用上一轮 MiniMax 的 `006_image.png`（5472 × 3072，50,436,942 字节）。
在真实 HTTP 块获取过程中注入一次暂时超时，再注入块 1 中断；首次保留块 0、2，
续传复用 16,777,216 字节（16 MiB），只传输余下 33,659,726 字节。
恢复阶段约 70.27 s，仍受网络影响；不是吞吐性能基准。
最终文件大小、完整 SHA-256 和 PIL PNG 校验通过，无重复 GPU 生成。
独立的最终验收读取文件进行比对，不代表生产下载路径会二次全量读取。

## 4. 验证与维护边界

PC2 最终回归 316 项测试、206 项子测试通过，CNB 14 项专项测试通过；
CNB 已完成真实探针部署及大文件断点恢复，但未重新运行 24 张图片的 GPU 推理，
未验证跨批次 Snapshot 恢复。不要将 CPU Ready 等同于 GPU 已就绪。
Volume V2 分块适配依赖 Modal SDK 1.6.4 的内部 RPC/块摘要契约，
SDK 升级必须重新测试真实传输、超时、损坏块、断点恢复以及 V1/公共流式回退路径。

| 关键实现 | 职责 |
| --- | --- |
| [deployment_probe.py](../runtime/serving/deployment_probe.py) | 专属名称、探针配置、完整版本断言 |
| [provisioning.py](../orchestration/provisioning.py) | 当前 App 清单校验、就绪状态、恢复与排空 |
| [deployment_state.py](../orchestration/deployment_state.py) | 跨平台源码身份 |
| [sdk.py](../orchestration/sdk.py) | GPU 句柄隔离与预期版本传递 |
| [deploy_shot.py](../pipeline/deploy_shot.py) | CPU 工作流版本入口 |
| [model_runtime.py](../runtime/serving/model_runtime.py) | GPU 执行前的版本保护 |
| [volume_download.py](../runtime/utils/volume_download.py) | 有界分块下载、恢复与原子发布 |
| [files.py](../runtime/utils/files.py) | 公共流式传输入口 |
| [专项测试](../tests/test_probe_volume_recovery.py) | 版本与断点恢复回归 |

复用这些设计时应优先消除错误的等待依赖、保留不可变身份与可验证证据，再考虑增加并发、延长超时或扩大资源。

## 5. SDK 会话复用与不可变首尾帧输入（2026-11-25）

- Runner 以进程内 SDKSession 管理有界 Call 句柄（256）、已验证输入及最近账本检查时间（各512）。模型句柄及缓存指纹按原身份复用，并发首次创建按键加锁；有完整部署哈希的工作流函数按专属 tag 复用，旧式未固定版本的函数仍逐次解析。部署版本保护、SubmissionLedger 原子保留和已提交任务不重放的原则不变。
- 已知在途 Call 的账本首查、每6秒复查，以及 Call 返回/异常后即时复查；无 call_id 或 uncertain 状态仍逐次读取权威账本。Call.get(timeout=0) 轮询频率不变。只缓存检查时间，不缓存可变执行状态。若结果在 Call 结束前写入，常规发现延迟可能增加至6秒加调度/网络耗时；Call 终止或异常不受该间隔限制。
- 首尾帧按原 SHA-256 路径 staging：先读取并校验本地快照，已存在对象验证大小及完整哈希，缺失才以 force=False 上传。并发冲突时仅接受 FileExistsError 或 SDK 1.6.4 针对精确目标路径的 InvalidError（真实返回 path "<key>" already exists），再校验获胜对象；不覆盖损坏对象。一次会话内相同已验证对象不重复传输，其他进程第一次复用仍校验字节。相同路径、摘要、大小的两帧在 GPU 输入解析只读取/编码一次；不同路径即使内容相同仍独立校验。
- 注意本仓库的 /inference/workflow-inputs 与模型及 model-results 共用 model-store；历史执行记录只有 intent_hash/execution_hash，没有完整持久化输入引用清单，也不能穷尽其他工作区或历史云端提交。因此先做只读依赖审计，未实现删除：

  ```powershell
  uv run python tools/audit_workflow_inputs.py --volume model-store --task-root .runtime/tasks --out outputs/workflow-input-audit.json
  ```

  可重复 --task-root 输入其他已知工作区任务目录。仅列举 /inference/workflow-inputs，校验同卷计划哈希，识别已扫描计划的未来/当前输入引用、未出现引用的对象、未上传的本地输入、缺失的显式云端输入及异常。unobserved_by_scanned_plans 不等于无依赖或可回收；deletion_allowed 永远为 false。不扫描权重、不下载媒体、不删除文件，不应将报告用于自动删除。真正的 GC 需要完整的持久化根清单、旧任务迁移、显式退休协议及独占维护/写入协调机制。

### 真实 SDK 隔离验收

基线为 ba55375；隔离 Volume V2（vo-gI4qbhcoiKrmsUvKKUEFEm）与 CPU App ap-ur67D4uvfvsEk3D3F2ZMKi。使用真实512×512 PNG及真实在途 CPU Call，未启动 GPU。

| 测试 | 优化前 | 优化后 |
| --- | --- | --- |
| 同帧4次 staging | batch_upload 4；2.781 s | batch_upload 1、首次 read_file 1；1.000 s |
| 同一在途Call 4次轮询 | read_file 4、from_id 4、get 4；4.985 s | read_file 1、from_id 1、get 4；2.078 s |
| 新会话已有输入 | 强制覆盖 | read_file 1，上传0 |

以上为单次实测 SDK 方法计数与墙钟耗时，不代表底层 RPC 数完全等同，也不是 GPU 生成或整体任务加速比例。真实并发争抢同一内容对象通过（首次发现 InvalidError 后修复并重验）；相同帧读取1次；只读依赖审计对3个对象列举1次、媒体读取0次，未来未上传输入正确报告。CPU Call 未退出时提交模拟结果与媒体，Runner 恢复已完成证据。最终334项测试和209项子测试通过（15项新增，18条现有SDK弃用警告）；实验App停止、容器0、隔离卷删除，生产卷和生产部署不变。实验记录保留在 outputs/sdk-input-lab-1125/。未做真实GPU推理、端到端媒体吞吐、Qwen LLM/KV Cache调参或历史工作流安全回收。
