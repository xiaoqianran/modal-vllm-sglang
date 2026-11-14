# Modal 图片 / 视频模型服务

所有模型都使用同一套公共实现：**model.json → CPU 准备权重 → Deploy → Task JSON → 生成结果**。
现有 MOSS、Pika 和 MiniMax 与以后新增的模型采用相同入口、任务格式和恢复流程。

```text
001-moss-image/          MOSS：model.json + 三个薄入口
002-pika-h3-flex/        Pika：同上，含文生视频与首尾帧能力
005-minimax-h3-image/    MiniMax：同上，配置独立文本编码器
shared/                 部署、缓存、参数校验、推理、下载与恢复的公共实现
tasks/                  提示词、镜头、seed、生成参数
inputs/                 输入素材
outputs/<project>/<scene>/  生成结果及 .runner 元数据
workflows/              单模型或跨模型步骤定义
orchestration/          私有 HTTP 工作流执行器
scripts/new_model.py    新模型目录生成器
templates/standard-model/model.json  生成器使用的配置模板
tests/                  全部离线测试
```

## 1. 模型目录

每个模型目录都只有以下维护文件（本地环境和 uv.lock 另计）：

```text
model.json              模型、镜像、启动命令、资源、缓存组件、能力与参数
app.py                  调用 shared.model_service.create_app
download_model.py       调用公共 CPU 下载及缓存检查
run.py                  调用公共 Task CLI
pyproject.toml          本地依赖
README.md               模型使用说明
```

参数默认值只在 model.json 中定义。跨字段约束放在 shared/params_*.py，SDK 与 HTTP 编排共用。
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
新模型默认 models/<service>/。首次升级先运行 CPU 下载入口（不加 --refresh）：
旧清单经模型身份、快照路径、必需文件及文件大小校验后就地转换，不复制或下载权重。
原清单保留为 model-ready.legacy.json；校验不通过时停止，不自动重新下载。
GPU 只接受新清单，因此必须先完成 CPU 转换再部署。详见 [升级说明](docs/unified-migration.md)。
Hugging Face 组件记录实际解析的 snapshot；分支引用首次准备后复用已固定的缓存。
显式更新或修复 HF 权重时使用 download_model.py --refresh；GPU 只验证和读取缓存，不下载权重。
Pika 使用引擎 Overlay 注册表物化基础组件，CPU 与 GPU 使用相同镜像。
启动时检查组件必需文件与文件大小清单；缓存清单指纹参与生成记录校验。

## 3. 只指定 Task 运行

```powershell
uv run python 001-moss-image/run.py --task tasks/003-stop-rain-001-rain-bus-stop.json --dry-run
uv run python 002-pika-h3-flex/run.py --task tasks/004-pika-video-demo.json --dry-run
uv run python 005-minimax-h3-image/run.py --task tasks/005-minimax-image-demo.json --dry-run
```

去掉 --dry-run 才提交真实 GPU 推理。三个入口均支持重复 --task、--limit 和 --output。
整批任务先完成离线校验再开始提交；--limit 不会绕过未选中条目的参数校验。
SDK 使用本机 Modal 账号，不需要 HTTP 地址或 Proxy Token。

统一 Task 格式：

```json
{
  "version": 1,
  "name": "006-demo",
  "project": "006-demo",
  "scene": "001-main",
  "workflow": "moss-t2i",
  "params": {"size": "1024x1024"},
  "items": [
    {"id": "001", "prompt": "A red paper boat at sunrise", "seed": 42}
  ]
}
```

workflow 可选 moss-t2i、minimax-t2i、pika-t2va、pika-fl2va。
params 覆盖顺序为模型默认值 → task params → item params。
不写 seed 时按任务名、能力和镜头 ID 稳定生成，调整条目顺序不改变 seed。
project 默认等于 name，scene 默认 001-main。相对素材路径以仓库根目录为基准。
首尾帧使用 {"file":"inputs/pika/001_first.png"}，参考 tasks/004-pika-frames-demo.json。

## 4. 结果与恢复

结果写入 outputs/<project>/<scene>/001.png 或 001.mp4。
每个镜头的 .runner 元数据记录请求签名、实际模型缓存指纹及结果 SHA256。
重复原命令：完整本地结果直接复用；缺失本地文件先从 Volume 恢复，缺失的镜头才提交 GPU。
添加新镜头不改变已有镜头的云端目录。已存在但与签名或哈希不一致的文件拒绝覆盖。

提交请求前保存状态；视频获得 job ID 后立即保存。查询或下载失败后继续同一个任务。
图片响应丢失、或视频提交后没有拿到 job ID 时停止自动重提。
Ctrl+C 停止本地客户端不等于取消云端任务。
SDK 在生成前校验客户端与已部署 model.json 的指纹，拒绝配置不一致的调用。

## 5. 跨模型组合与新增模型

MOSS 首尾帧 → Pika 视频等组合使用 python -m orchestration plan/run/resume/status。
需要配置相应的 *_ENDPOINT 和 *_MODAL_KEY / *_MODAL_SECRET；见 [工作流说明](docs/workflow-runner.md)。
能力从各模型 model.json 自动发现，无需为每个模型修改编排器。

新增模型使用 [接入指南](docs/add-model.md)，通常只需要配置 model.json。

## 6. 验证与依赖

```powershell
uv run python -m unittest discover -s tests -v
```

离线测试覆盖参数、配置发现、Modal 注册、缓存组件、首尾帧、下载校验和故障恢复，不调用 GPU。
本轮统一迁移没有执行云端 Deploy 或真实 GPU 验收；本地代码更新不会改变已部署服务。
MOSS/Pika 镜像延续现有版本选择，Pika dev 镜像仍是可变标签，生产使用应固定经过验收的 digest。

根目录和各模型使用 pyproject.toml 声明依赖，uv.lock 仅保存在本机、不提交 Git。
