# modal-vllm-sglang 图片 / 视频模型服务

自动准备入口：登记 Task 后运行 worker，即可按需下载权重、构建镜像、部署模型及云端编排服务。

```powershell
uv run python -m orchestration run tasks/006-moss-pika-demo.json
uv run python -m orchestration worker
```

worker 自动扫描 `*/model.json`，默认最多并行准备两个服务；新模型无需额外注册准备流程，自动路径通过
`orchestration/provision_entry.py` 使用通用工厂，不依赖模型目录内的薄入口。
任务仍需在 workflow 中引用模型能力并描述输入依赖；特殊推理协议仍需相应适配代码。
准备期间云端 job 显示 `blocked`、`wait_reason=model`，已经就绪的模型可以先生成。
CPU 下载器检查完整文件清单、复用有效缓存，自动路径会修复校验失败的缓存；部署版本包含源码、配置和权重指纹，
通过 CPU 元数据函数核对，GPU 随实际生成启动。首次接管手动部署会更新为受版本管理的部署。

准备失败最多尝试三次，原因保存在 Modal Dict `modal-vllm-sglang-orchestration-provisioning`。
完成准备前需保持 worker 运行；正常退出会等待后台准备结束。准备完成后云端生成可独立继续。
共享锁防止多 worker 同时准备同一应用；强制杀进程或机器掉电可能遗留 `lock:<app_name>`，
需确认原准备进程及其云端下载已停止后清理该锁再重启，系统不会按超时强抢锁。
模型配置或源码修改后重启 worker 重新核对版本。固定模型 revision 与镜像版本可保证更稳定的复现。

`shared/tasks/task_spec.py` 统一任务加载、参数准备、签名、远端结果前缀及本地任务身份。
部署服务和异步编排器共同依赖它。所有业务任务统一交给 orchestration，模型目录不再提供 run.py。

通用 GPU 快照：MOSS、Pika、MiniMax 共用拍摄与恢复生命周期，保留 30 秒缩容。
配置、计时边界及逐引擎远程验收要求见 [快照说明](docs/memory-snapshots.md)。

运行记录统一在 `shared/telemetry/`：登记、提交、等待、启动、生成、保存、素材交接、下载使用固定阶段名。
后台保存事件、采样 CPU / 内存 / 显存，独立收集并输出 Task 报告；详见 [telemetry 说明](docs/telemetry.md)。

```powershell
uv run python -m orchestration telemetry collect
uv run python -m orchestration telemetry report outputs/006-moss-pika-demo/001-main
```

所有任务采用同一种异步方式：run 登记后返回，worker 持续提交与查询，下载在线程池中独立进行。
同一 job 的提交互斥，状态文件只短暂加锁；视频等待不阻挡其他任务入队。

所有模型都使用同一套公共实现：**model.json → CPU 准备权重 → Deploy → Task JSON → 生成结果**。
现有 MOSS、Pika 和 MiniMax 与以后新增的模型采用相同入口、任务格式和恢复流程。

```text
001-moss-image/          MOSS：model.json + 两个薄入口
002-pika-h3-flex/        Pika：同上，含文生视频与首尾帧能力
005-minimax-h3-image/    MiniMax：同上，配置独立文本编码器
shared/                 四个按职责组织的公共包
  config/                模型配置、权重缓存、参数校验
  serving/               Modal GPU 服务、推理 API、网关及运行时
  tasks/                 任务解析、提交、恢复与批次处理
  utils/                 文件、媒体格式、HTTP 与输出路径
tasks/                  提示词、镜头、seed、生成参数
inputs/                 输入素材
outputs/<project>/<scene>/  生成结果及 .runner 元数据
workflows/              单模型或跨模型步骤定义
orchestration/          统一 SDK 异步任务入口、依赖调度及云端素材交接
scripts/new_model.py    新模型目录生成器
templates/standard-model/model.json  生成器使用的配置模板
tests/                  全部离线测试
```

## 1. 模型目录

每个模型目录都只有以下维护文件（本地环境和 uv.lock 另计）：

```text
model.json              模型、镜像、启动命令、资源、缓存组件、能力与参数
app.py                  调用 shared.serving.model_service.create_app
download_model.py       调用公共 CPU 下载及缓存检查
pyproject.toml          本地依赖
README.md               模型使用说明
```

参数默认值只在 model.json 中定义。跨字段约束放在 shared/config/params_*.py，任务与引擎请求共用。
Pika 的 SGLang Overlay、容器内首尾帧转换，MiniMax 的双组件下载，均由公共配置能力处理。

## 2. 准备与部署

以下命令均在仓库根目录执行，先确保本地已登录 Modal：

```powershell
uv sync
# CPU 准备模型；以 MOSS 为例
uv run python -m modal run 001-moss-image/download_model.py
# CPU 检查已有缓存
uv run python -m modal run 001-moss-image/download_model.py --check-only
# 发布 GPU 服务
uv run python -m modal deploy 001-moss-image/app.py
```

Pika、MiniMax 替换目录名执行同样命令。普通任务无需重复 Deploy。
修改 model.json 或 shared 中的云端代码后，需要重新 Deploy。
三个模型保留部署 Class 名称 Moss、Pika、H3，使用同一套公共实现；新模型默认 Model。
单容器内 SDK 批次串行；私有 HTTP 与 SDK 共用生成网关，但网关不额外限制生成并发。
查询与下载可以独立转发。POST 不自动重试；提交结果不明时，由各任务日志阻止该任务重复提交。
没有任务时允许缩容至零，下次调用仍可能需要加载模型。

现有模型保留旧 Volume 缓存目录：007-moss-image、009-pika-h3-flex、003-minimax-h3-image。
新模型默认 models/<service>/。所在 Volume 统一为 modal-vllm-sglang。
新盘为空时先运行普通 CPU 准备命令；旧 modal-forge Volume 不会自动复制到新盘。
只有同一盘已有旧清单时才用 --migrate-only，它只校验并转换，不下载权重。
--check-only、--refresh、--migrate-only 互斥；GPU 只接受已准备好的新清单。
Hugging Face 组件记录实际解析的 snapshot；分支引用首次准备后复用已固定的缓存。
显式更新或修复 HF 权重时使用 download_model.py --refresh；GPU 只验证和读取缓存，不下载权重。
CPU 模型下载默认把 Hugging Face `snapshot_download()` 的文件工作线程设置为 8；
Pika 保留 SGLang Overlay 物化过程，只约束其内部调用的快照下载。此项与
图片/视频结果收集的 8 线程、以及 Xet 内部分块并发相互独立。
CPU 下载会检查 Modal Secret `huggingface-secret`：存在且包含 `HF_TOKEN` 时
自动注入 Hugging Face 身份认证；Secret 不存在则匿名下载，无需设置本地 Token。
其他 Secret 访问错误会直接报错，避免悄悄降级。正在运行的下载容器不会热更新。
Pika 使用引擎 Overlay 注册表物化基础组件，CPU 与 GPU 使用相同镜像。
启动时检查组件必需文件与文件大小清单；缓存清单指纹参与生成记录校验。

## 3. 统一提交 Task

Task 的 workflow 决定模型和依赖，不需要选择模型目录里的命令：

| workflow | 行为 |
| --- | --- |
| moss-t2i | MOSS 图片 |
| minimax-t2i | MiniMax 图片 |
| pika-t2va / pika-fl2va | Pika 文生视频 / 本地首尾帧视频 |
| storyboard-to-video | MOSS 首尾帧 → Pika 视频 |

```powershell
# 离线校验，不连接 Modal、不写运行记录
uv run python -m orchestration plan tasks/006-moss-pika-demo.json
# 登记后立即返回，不等待生成或下载
uv run python -m orchestration run tasks/006-moss-pika-demo.json
# 一次登记多份单模型或组合任务
uv run python -m orchestration run tasks/004-pika-video-demo.json tasks/005-minimax-image-demo.json
```

run 只有异步登记这一种行为，无需 --enqueue，也没有同步等待模式。
使用本机 Modal 账号；无需 HTTP 地址或 Proxy Token。一次注册前先离线校验所有 Task。
统一格式示例：

```json
{
  "version": 1,
  "name": "006-demo",
  "workflow": "moss-t2i",
  "items": [{"id": "001", "prompt": "A red paper boat at sunrise", "seed": 42}]
}
```

文件名需与 name 一致。project 默认 name，scene 默认 001-main。
参数覆盖为模型默认值 → task params → item params；多步骤的参数写在 workflow step 上。
省略 seed 时稳定生成；本地首尾帧使用 {"file":"inputs/pika/001_first.png"}。

## 4. 执行、追加和恢复

另一个终端保持 worker 运行：

```powershell
uv run python -m orchestration worker
```

worker 发现已登记的 SDK 计划后才提交付费生成；run 本身不访问云端。
随时再执行 run 登记其他 Task，或保留旧镜头定义、追加新镜头后重新 run。
MOSS 和 Pika 各自串行生成，两个独立 GPU 容器可重叠运行。
某镜头的首尾帧在云端 Volume 完成后即可提交视频，本地八线程下载不构成依赖。

```powershell
# 查看最后一次保存的状态，不访问云端
uv run python -m orchestration status outputs/006-moss-pika-demo/001-main
# 通知运行中的 worker 继续查询原调用，并补下载；不重复生成
uv run python -m orchestration resume outputs/006-moss-pika-demo/001-main
```

状态权威为 outputs/<project>/<scene>/state.json，plan.json 冻结原始计划。
云端 artifacts 与本地 local_artifact 分开保存。提交结果不明时不自动重新生成。
关闭 worker 不取消已提交的云端工作，但新的依赖任务需要 worker 恢复后才会提交。
同模型持续排队通常能复用热容器；确实空闲后仍允许 30 秒缩容，并从快照恢复。
详见 [工作流说明](docs/workflow-runner.md)。

## 5. 新增模型

使用 [接入指南](docs/add-model.md)。生成器创建模型配置、部署/下载入口、单模型 workflow 与
符合统一命名的示例 Task；所有新模型也直接使用 orchestration，不再生成独立 run.py。

## 6. 验证与依赖

```powershell
uv run python -m unittest discover -s tests -v
```

离线测试覆盖参数、配置发现、Modal 注册、缓存组件、首尾帧、下载校验和故障恢复，不调用 GPU。
本轮统一迁移没有执行云端 Deploy 或真实 GPU 验收；本地代码更新不会改变已部署服务。
MOSS/Pika 镜像延续现有版本选择，Pika dev 镜像仍是可变标签，生产使用应固定经过验收的 digest。

根目录和各模型使用 pyproject.toml 声明依赖，uv.lock 仅保存在本机、不提交 Git。

## 7. 后续云端验收入口（本次未执行）

```powershell
# 仅 CPU，迁移已有缓存；不会下载模型权重
uv run python -m modal run 002-pika-h3-flex/download_model.py --migrate-only
uv run python -m modal run 005-minimax-h3-image/download_model.py --migrate-only
# 部署后进行实时自检：会启动 GPU、加载模型，但不生成媒体
uv run python -m orchestration check-deployment pika
uv run python -m orchestration check-deployment minimax
# 只查看自检计划，无云端访问
uv run python -m orchestration check-deployment pika --dry-run
```

自检校验部署配置签名、缓存指纹及异步接口版本，输出请求总耗时和容器内启动耗时。
旧部署没有 `deployment_info()` 时会报错，需要重新部署；自检不能代替真实生成和断线恢复验收。

`model_ready`、`generate_one_completed`、`batch_item_completed` 记录启动/执行耗时和资源快照。
后台采样间隔由 `telemetry_interval` 控制，默认 5 秒。RAM 和 CPU 使用 Linux cgroup v2 的
容器统计（含引擎子进程）；显存使用 nvidia-smi，记录采样峰值，可能漏掉短暂尖峰。
不支持的统计字段省略，采集不影响生成成功与否。资源峰值是容器生命周期统计，不是单请求独占统计。
`startup_seconds` 从容器进入初始化开始计时，不含镜像拉取和平台排队；自检的
`request_seconds` 包含客户端等待。结果元数据的 `execution_seconds` 包含请求、等待及响应获取，
恢复视频时只代表本次恢复执行时间，不是模型纯推理速度。
`download_completed` 输出首块等待、字节数、耗时和吞吐；`collect_summary` 输出批次吞吐。
同步生成期间的收集统计包含等待生成结果的时间，比较纯下载性能请使用已完成任务的 `--collect --remote`。

本次保留 Pika/MiniMax 的 CPU、RAM 配置。Pika 镜像仍为 dev：当前没有经过验证的
Pika digest，不应把 MiniMax 的 digest 直接套用。获得验证版本后修改 Pika 的 `image`
为 `lmsysorg/sglang@sha256:<digest>`，CPU 准备和 GPU 服务会共同采用该镜像；配置签名
也随之改变，因此必须重新部署并使用新的任务身份验收。
