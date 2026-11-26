# modal-vllm-sglang 图片 / 视频模型服务

### 按执行顺序读代码

| 阶段 | 位置 | 运行位置与职责 |
| --- | --- | --- |
| 01 | [构建计划](pipeline/s01_build_plan.py) | 本地读取任务和工作流，登记分镜 |
| 02 | [提交分镜](pipeline/s02_submit_shots.py) | 本地 worker 选择直连 GPU 或云端依赖编排，并保存调用回执 |
| 03 | [执行分镜](pipeline/s03_run_shot.py) | 复用步骤调度：独立任务在本地推进，依赖任务在云端推进 |
| 04 | [收集产物](pipeline/s04_collect_outputs.py) | 本地 worker 下载已完成的产物并更新报告 |

用 `uv run python run.py plan tasks/004-pika-frames-demo.json` 离线查看计划。
`run` 登记任务，`worker` 负责推进。02 对独立任务直接调用 GPU SDK，对依赖任务启动云端 03，并在轮询时调用 04；多个分镜可以并行。

### 共享模型 Volume V2（渐进迁移）

普通 Hugging Face 模型已支持 `model.json` 中按服务启用 `"model_storage": "shared"`：
通用权重只读挂载在 `/mnt/models`，原有服务输出、`model-ready.json` 与遥测仍使用
`/mnt/modal`。未启用的正式模型完全沿用旧缓存；Pika Overlay 暂不切换。
已经使用 ComfyUI 模型快照做过零复制接管与 CPU 跨容器校验，另已验证 benchmark
直接下载到 V2 模型卷并由全新 CPU 容器读取。尚未将大型生产模型整体迁移或完成 GPU 验收。
详细的两条迁移路径、逐服务切换与回退操作见
[共享模型存储迁移](docs/shared-model-rollout.md)。

### 分辨率预设（代码为准）

`runtime/config/resolution.py` 是统一预设来源；可离线查看全部比例及模型原生视频画布：

```powershell
uv run python run.py resolutions
```

**默认图片档位为 1K（长边1024）**，默认比例1:1；仅写 `"render": {"aspect_ratio": "16:9"}` 即为1024×576。
对于原生默认画布为 1024×1024 的图片模型，未指定尺寸时使用1K；其他模型优先保留各自 `model.json` 的原生默认画布。
显式的 workflow/Task/item 尺寸仍优先保留。MiniMax 默认2K方图；当前仓库参数校验允许 1–20 MP 且宽高须为32的倍数，这只是请求校验范围，不代表各尺寸均经过引擎/质量验收。
视频链沿用明确配置的模型原生画布，不把480p伪称为默认1K；显式请求模型不支持的1K视频会在登记前报错。

在 Task JSON 顶层增加以下字段，替代手填图片像素：

```json
"render": {"aspect_ratio": "16:9", "resolution": "512"}
```

图片支持 `256`、`512`、`768`、`1k`、`2k`、`4k`，分别表示长边 256、512、768、1024、2048、4096。
`pseudo1k`、`pseudo-1k`、`伪1k` 都是 `768` 的别名，仅属于项目命名，不是行业标准。
支持比例 `1:1`、`3:4`、`4:3`、`9:16`、`16:9`。例如：

| 档位 | 16:9 | 9:16 | 4:3 | 3:4 | 1:1 |
|---|---|---|---|---|---|
| 256 | 256×144 | 144×256 | 256×192 | 192×256 | 256×256 |
| 512 | 512×288 | 288×512 | 512×384 | 384×512 | 512×512 |
| 768 / 伪1K | 768×432 | 432×768 | 768×576 | 576×768 | 768×768 |
| 1K | 1024×576 | 576×1024 | 1024×768 | 768×1024 | 1024×1024 |
| 2K | 2048×1152 | 1152×2048 | 2048×1536 | 1536×2048 | 2048×2048 |
| 4K | 4096×2304 | 2304×4096 | 4096×3072 | 3072×4096 | 4096×4096 |

建议：256/512 用于构图草稿，768 用于快速预览，1K 用于常规图片，2K/4K 按模型能力与资源选择。
档位只描述尺寸，不保证低分辨率的细节表现，也不把像素数等同于画质。
MOSS 仍须满足 16 像素对齐；MiniMax 仍须满足 32 像素对齐和 1–20 MP。官方建议范围与仓库校验边界、预设可用性并不等价，详见[各模型分辨率与尺寸目录](docs/model-resolution-catalog.md)。
模型验证失败会在登记前报错，不静默改尺寸或降低档位。新增模型可在自己的 `model.json` 声明
`resolution_profiles`，无需另建服务注册表。

图生视频任务的 `render` 必须选择该模型声明的原生档位。当前 SGLang Pika 配置声明了 `480p`，以及 16:9 的 `768p` 高画质档位：

```json
"render": {"aspect_ratio": "16:9", "resolution": "480p"}
```

当前仓库配置同步选择视频目标和 864×480 首尾帧；1K/2K/4K 不是这条视频链的原生预设。该画布是本地配置声明，不等同于上游保证的尺寸。
`render.resolution="768p"` 与 `aspect_ratio="16:9"` 同步选择 1344×768 首尾帧和视频目标；其他 768p 比例暂不声明。预热固定为 480p，不改变真实请求的分辨率、时长及六步采样。
视频模型的原生画布以当前版本 `model.json` 中的映射为准，不直接套用其他后端的桶位表。
当前没有接入自动超分或低档导出流程；不支持的预设不会冒充已支持。

Task 级 `render` 覆盖 workflow 的默认尺寸，与 Task/item 的 `params.size`、`params.target` 不能同时使用。
不填写 `render` 时，纯图片无显式尺寸的任务使用各模型默认尺寸（MOSS 为1K，MiniMax 为2K），视频链保持已有工作流配置。分辨率不同的计划有不同执行签名；已登记任务仍保留原尺寸，
不会因修改预设自动覆盖历史结果。MOSS 的 2K/4K 档位本次仅做离线参数验证，尚未进行 GPU 质量及显存验收。

### 本地成品与监控

日常只打开 `outputs/监控<任务名>.html` 查看对应任务，或到 `outputs/<任务名>/` 获取成品。
文件直接平铺为 `001_first.png`、`001_last.png`、`001_video.mp4`（单图步骤为 `001_image.png`）。
004 首尾帧工作流使用 `864x480` 图片，对应当前 Pika 480p、16:9 的实际输出画布。
登记前会检查生成首尾帧的短边、方向、宽高比及首尾尺寸一致性；发现不匹配会拒绝登记。
已有成品不会随配置修改重绘，监控页显示实际分辨率，并标出首尾帧与视频的尺寸差异。
不会再自动生成 outputs-debug 或每镜头子目录。

程序文件统一放在 `.runtime/`：

* `tasks/<project>/<scene>/`：任务快照、计划、状态、调用 ID、输入快照及本地路径映射。
* `models/<service>.json`：共用准备状态、部署版本和缓存指纹。
* `logs/worker.log`：worker 日志，8 MB 轮转，保留三份历史。
* `logs/tasks/`：每任务状态变化事件；`logs/models/`：下载服务构建、部署日志。
* `logs/.telemetry/`：已有结构化运行记录、指标索引和报告；远程详细下载日志仍可通过 Modal 查看。

`worker` 每五秒更新本地监控数据；页面自动读取 `.runtime/monitor/<任务名>.js`，无需本地 HTTP 服务。
手动刷新与自动刷新都只更新状态和耗时，不替换未变化的视频节点，保留播放位置和已展开的详情。
监控展示任务及镜头已观测耗时、登记/提交/等待/启动/生成/云端保存/素材交接/下载阶段、实际尺寸、
文件大小、已采集的下载字节与平均速度、提示词、参数和调用ID。任务总耗时按墙钟边界计算，不将并行阶段相加。
模型准备记录共用CPU准备服务、权重下载与校验、缓存指纹检查、推理构建部署的组合耗时，失败尝试也保留。
独立的构建/部署边界、权重下载GB与速度、平台排队和快照恢复尚未采集时显示“未采集”；不推测数据。
新任务保存登记、步骤完成与本地收集边界；历史记录显示“从首次已知提交计算”，不会把迁移或缓存复核当作新完成。
页面支持手机布局、关闭自动更新及检查worker数据是否过期；详细阶段默认折叠，成品按镜头集中展示。
恢复和状态命令可以直接指定 `outputs/<任务名>`；同名任务有多个场景时需指定 `.runtime/tasks/<project>/<scene>`。
重启继续使用原调用 ID。当前只维护 `.runtime/tasks/` 状态与 `outputs/` 成品布局，旧目录迁移工具已移除；已有历史调试目录保存在 `.runtime/legacy/`。

自动准备入口：登记 Task 后运行 worker，即可按需下载权重、构建镜像、部署模型及云端编排服务。

```powershell
uv run python run.py run tasks/006-moss-pika-demo.json
uv run python run.py worker
```

worker 自动扫描 `*/model.json`，默认最多并行准备两个服务；新模型无需额外注册准备流程，自动路径通过
`orchestration/provision_entry.py` 使用通用工厂，不依赖模型目录内的薄入口。
每个模型按需使用同名 `prepare-<service>` CPU 服务；有效的 Volume 缓存可直接复用，无需重部署 CPU 下载服务；缓存失效时才更新同名服务，
下载重试复用服务，不再通过 `modal run` 反复创建临时 App。每个准备服务最多一个下载容器。
任务仍需在 workflow 中引用模型能力并描述输入依赖；特殊推理协议仍需相应适配代码。
准备期间 job 显示 `blocked`、`wait_reason=model`，已经就绪的模型可以先生成。
CPU 下载器检查完整文件清单、复用有效缓存，自动路径会修复校验失败的缓存；部署版本包含源码、配置和权重指纹，
通过 CPU 元数据函数核对，GPU 随实际生成启动。首次接管手动部署会更新为受版本管理的部署。

准备失败最多尝试三次，原因保存在 Modal Dict `modal-vllm-sglang-orchestration-provisioning`。
失败记录在 worker 重启后仍保持，避免无休止重试；版本校验超时例外：新批次可以核对源码、缓存指纹、远端版本及旧版本排空状态，完全一致时恢复 Ready，不重新部署。其他失败确认根因后通过 `run.py prepare --retry <service>` 显式重新准备。
完成准备前需保持 worker 运行；正常退出会等待后台准备结束。准备完成后云端生成可独立继续。
共享锁防止多 worker 同时准备同一应用；强制杀进程或机器掉电可能遗留 `lock:<app_name>`，
需确认原准备进程及其云端下载已停止后清理该锁再重启，系统不会按超时强抢锁。
模型配置或源码修改后重启 worker 重新核对版本。固定模型 revision 与镜像版本可保证更稳定的复现。

004 分镜的 `tasks/004-pika-frames-demo.json` 是独立维护的多模型工作流任务；`tools/generate_storyboard.py` 只维护 `docs/004-before-rain/` 下的四份提示词文档与图片清单，不会覆盖任务 JSON。后续编辑镜头须分别检查任务与文档的一致性。

`pipeline/s01_build_plan.py` 统一加载任务并构建执行计划；`runtime/tasks/task_spec.py` 负责推理条目校验、签名与远端结果前缀。
部署服务和异步编排器共同依赖它。所有业务任务统一通过 worker：单模型单步骤的独立场次直连 GPU SDK，复合依赖场次交给云端 run_shot；模型目录不再提供 run.py。
旧单模型客户端 `run_jobs`、同步批处理 `run_task` / `generate_batch_stream` 和 HTTP 工作流执行器已移除；统一使用 `run` 登记任务，再由 `worker` 执行。私有 HTTP 推理服务保留。

通用 GPU 快照：MOSS、Pika、MiniMax 共用拍摄与恢复生命周期；Pika/MiniMax 空闲缩容 60 秒，MOSS 30 秒。
非快照模式按需加载 GPU 引擎，先检查完整产物；新模型模板默认 off，已有明确 gpu 配置保持不变。
配置、计时边界及逐引擎远程验收要求见 [快照说明](docs/memory-snapshots.md)。

运行记录统一在 `runtime/telemetry/`：登记、提交、等待、启动、生成、保存、素材交接、下载使用固定阶段名。
后台保存事件、采样 CPU / 内存 / 显存，独立收集并输出 Task 报告；详见 [telemetry 说明](docs/telemetry.md)。

```powershell
uv run python run.py telemetry collect
uv run python run.py telemetry report outputs/006-moss-pika-demo/001-main
```

所有任务采用同一种异步方式：run 登记后返回，worker 持续提交与查询，下载在线程池中独立进行。
同一 job 的提交互斥，状态文件只短暂加锁；视频等待不阻挡其他任务入队。

所有模型都使用同一套公共实现：**model.json → CPU 准备权重 → Deploy → Task JSON → 生成结果**。
现有 MOSS、Pika 和 MiniMax 与以后新增的模型采用相同入口、任务格式和恢复流程。

```text
models/001-moss-image/          MOSS：model.json + 两个薄入口
models/002-pika-h3-flex/        Pika：同上，含文生视频与首尾帧能力
models/005-minimax-h3-image/    MiniMax：同上，配置独立文本编码器
runtime/                 四个按职责组织的公共包
  config/                模型配置、权重缓存、参数校验
  serving/               Modal GPU 服务、推理 API、网关及运行时
  tasks/                 任务解析、提交、恢复与批次处理
  utils/                 文件、媒体格式、HTTP 与输出路径
tasks/                  提示词、镜头、seed、生成参数
inputs/                 输入素材
outputs/<project>/<scene>/  生成结果及 .runner 元数据
workflows/              单模型或跨模型步骤定义
orchestration/          统一 SDK 异步任务入口、依赖调度及云端素材交接
tools/new_model.py    新模型目录生成器
templates/standard-model/model.json  生成器使用的配置模板
tests/                  全部离线测试
```

## 1. 模型目录

每个模型目录都只有以下维护文件（本地环境和 uv.lock 另计）：

```text
model.json              模型、镜像、启动命令、资源、缓存组件、能力与参数
app.py                  调用 runtime.serving.model_runtime.create_app
download_model.py       调用公共 CPU 下载及缓存检查
pyproject.toml          本地依赖
README.md               模型使用说明
```

参数默认值只在 model.json 中定义。模型专属参数约束放在各模型的 model_impl/ 或 model_impl/params.py，
由 validator_module 声明并由任务与引擎请求共同使用。
Pika 的 SGLang Overlay 放在 model_impl/；容器内首尾帧转换和 MiniMax 双组件缓存则使用公共基础设施。

## 2. 准备与部署

以下命令均在仓库根目录执行，先确保本地已登录 Modal：

```powershell
uv sync
# 免费检查下载计划；以 MOSS 为例
uv run python models/001-moss-image/download_model.py
# 显式开始 CPU 下载（会产生云端费用）
uv run python models/001-moss-image/download_model.py --execute
# CPU 检查已有缓存，不下载（会启动小型 CPU 容器）
uv run python models/001-moss-image/download_model.py --execute --check-only
# 发布 GPU 服务
uv run python -m modal deploy models/001-moss-image/app.py
```

Pika、MiniMax 替换目录名执行同样命令。普通任务无需重复 Deploy。
修改 model.json 或 shared 中的云端代码后，需要重新 Deploy。
三个模型保留部署 Class 名称 Moss、Pika、H3，使用同一套公共实现；新模型默认 Model。
单容器内 SDK 单任务调用串行；私有 HTTP 与 SDK 共用生成网关，但网关不额外限制生成并发。
查询与下载可以独立转发。POST 不自动重试；提交结果不明时，由各任务日志阻止该任务重复提交。
没有任务时允许缩容至零，下次调用仍可能需要加载模型。

三个模型的新缓存目录统一为：/data/models/001-moss-image、/data/models/002-pika-h3-flex、/data/models/005-minimax-h3-image。旧目录 007-moss-image、009-pika-h3-flex、003-minimax-h3-image 不会自动迁移或删除。
新模型默认 models/<service>/。所在 Volume 统一为 model-store（Volume V2）。
新盘为空时先运行普通 CPU 准备命令；旧 modal-forge Volume 不会自动复制到新盘。
只有同一盘已有旧清单时才用 --execute --migrate-only，它只校验并转换，不下载权重。
--check-only 和 --migrate-only 互斥；统一 benchmark 入口不提供旧版 --refresh，GPU 只接受已准备好的新清单。
Hugging Face 组件记录实际解析的 snapshot；分支引用首次准备后复用已固定的缓存。
需要替换已有模型快照时，应先核实原缓存与具体版本，再单独执行可恢复的 benchmark 下载；不可直接用旧 --refresh。
模型下载统一交给 modal-benchmark 的可恢复 Writer 调度；Pika 保留 SGLang Overlay 物化过程。此项与
图片/视频结果收集的 8 线程、以及 Xet 内部分块并发相互独立。
CPU 下载会检查 Modal Secret `huggingface-secret`：存在且包含 `HF_TOKEN` 时
自动注入 Hugging Face 身份认证；Secret 不存在则匿名下载，无需设置本地 Token。
其他 Secret 访问错误会直接报错，避免悄悄降级。正在运行的下载容器不会热更新。
Pika 使用引擎 Overlay 注册表物化基础组件，CPU 物化器与 GPU 推理镜像独立固定版本；二者必须保持权重布局兼容。
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
uv run python run.py plan tasks/006-moss-pika-demo.json
# 登记后立即返回，不等待生成或下载
uv run python run.py run tasks/006-moss-pika-demo.json
# 一次登记多份单模型或组合任务
uv run python run.py run tasks/004-pika-video-demo.json tasks/005-minimax-image-demo.json
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
uv run python run.py worker
```

worker 发现已登记的 SDK 计划后才提交付费生成；run 本身不访问云端。
随时再执行 run 登记其他 Task，或保留旧镜头定义、追加新镜头后重新 run。
MOSS 和 Pika 各自串行生成，两个独立 GPU 容器可重叠运行。
某镜头的首尾帧在云端 Volume 完成后即可提交视频，本地八线程下载不构成依赖。

```powershell
# 查看最后一次保存的状态，不访问云端
uv run python run.py status outputs/006-moss-pika-demo/001-main
# 通知运行中的 worker 继续查询原调用，并补下载；不重复生成
uv run python run.py resume outputs/006-moss-pika-demo/001-main
```

状态权威为 outputs/<project>/<scene>/state.json，plan.json 冻结原始计划。
云端 artifacts 与本地 local_artifact 分开保存。提交结果不明时不自动重新生成。
关闭 worker 不取消已提交的云端工作，但新的依赖任务需要 worker 恢复后才会提交。
同模型持续排队通常能复用热容器；空闲后按模型配置缩容，后续请求可能恢复已有快照，也可能在新 Worker 类型上重新捕获。
详见 [工作流说明](docs/workflow-runner.md)。 版本专属探针、跨平台源码身份及大文件恢复的设计与实测见 [运行时可靠性设计](docs/runtime-reliability-design.md)。

## 5. 新增模型

使用 [接入指南](docs/add-model.md)。生成器创建模型配置、部署/下载入口、单模型 workflow 与
符合统一命名的示例 Task；所有新模型也直接使用 orchestration，不再生成独立 run.py。

## 6. 验证与依赖

```powershell
uv run python -m unittest discover -s tests -v
```

离线测试覆盖参数、配置发现、Modal 注册、缓存组件、首尾帧、下载校验和故障恢复，不调用 GPU。
本轮原生镜像迁移已完成三个媒体模型的真实 GPU 单项生成验收；未覆盖线上部署，GPU Snapshot 恢复与完整生产负载仍需单独验收。
MOSS、Pika、MiniMax 默认采用 Modal 原生镜像 + 官方固定版本 wheel + 匹配 GPU 的预编译 CUDA 内核；仅在原生方案无法验收时保留已固定的 registry 镜像。详见 [镜像构建规范和验收记录](docs/image-build-policy.md)。

根目录和各模型使用 pyproject.toml 声明依赖，uv.lock 仅保存在本机、不提交 Git。

## 7. 后续云端验收入口（本次未执行）

```powershell
# 仅 CPU，迁移已有同盘旧缓存；不会下载模型权重
uv run python models/002-pika-h3-flex/download_model.py --execute --migrate-only
uv run python models/005-minimax-h3-image/download_model.py --execute --migrate-only
# 部署后进行实时自检：会启动 GPU、加载模型，但不生成媒体
uv run python run.py check-deployment pika
uv run python run.py check-deployment minimax
# 只查看自检计划，无云端访问
uv run python run.py check-deployment pika --dry-run
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

保留 Pika/MiniMax 的 GPU 容器 CPU、RAM 配置。两者的 `model.json` 共享固定 SHA-256 的官方 SGLang Nightly wheel 与 CUDA wheel 兼容修复；Pika 另装 FFmpeg。Pika 下载阶段使用 `modal-benchmark` 轻量 CPU Worker，Overlay 物化阶段使用独立的 `debian_slim + uv` CPU 物化器，不复用 GPU 镜像。已有物化权重已通过真实 GPU 视频生成验收；重新物化、GPU snapshot 恢复及部署签名仍须独立验收。

Modal 多云 Worker 模型准备入口见 [接入说明](docs/model-download.md)；
公共代码与各模型实现的命名规则见 [模块命名约定](docs/module-conventions.md)。

### Modal V2 精确挂载

- 唯一模型 Volume：`model-store`（V2）。
- 本仓库云端数据放在 Volume 的 `/inference/` 子树；GPU、CPU 物化、缓存维护和 HF 归档容器只将这一子树挂载在容器的 `/data`。
- 容器内部模型仍在 `/data/models/<模型目录>`，结果在 `/data/model-results/`，不需要更改服务内业务路径。
- SDK 直接访问 Volume 的原始路径时，统一通过 `runtime.config.storage.volume_key()` 补齐 `/inference` 前缀，避免误读 ComfyUI 数据。
- `modal-benchmark==0.4.4` 已支持 `sub_path="/inference"`：下载 Worker、Coordinator 和业务发布容器均精确挂载该子树到 `/data`，不再由业务层转换下载路径。
- 切换子树不是旧卷数据迁移，原 Volume 根目录下的旧数据不会自动移动；新配置要求在子树内重新下载/准备。


### MiniMax H3 Image：12 个电影镜头与 HF 画廊

`tasks/022-minimax-cinematic-atlas-12.json` 是独立的电影感 12 镜头生成任务，16:9、2K，
每个镜头一个不同的原创 Prompt 和固定 Seed，HF-only 交付。
`tasks/022-minimax-cinematic-atlas-12.titles.json` 记录画面标题。

```powershell
python run.py prepare minimax
python run.py run tasks/022-minimax-cinematic-atlas-12.json
python run.py worker --task 022-minimax-cinematic-atlas-12 --poll 5
python -m tools.build_cinematic_gallery --strict
```

HF 图片从 Modal `model-store` 的 `/inference` 子目录经 CPU 归档直接上传
`seachen/modal-vllm-sglang-assets`，**不经过 PC2 本地文件**。
独立成品页面：`outputs/022-minimax-cinematic-atlas-12-gallery.html`。
画廊仅从已成功归档的真实 HF URL 构建；未完成的画面明确显示占位，
`--strict` 要求 12 张全部发布后才返回成功。
`worker --task` 仅调度指定 Task，不恢复仓库历史任务。

第一次尝试的 021 批次绑定旧版推理部署、未执行成功；保留作审计。022 是在新部署已验证后创建的独立任务，避免重放旧云端调用。
