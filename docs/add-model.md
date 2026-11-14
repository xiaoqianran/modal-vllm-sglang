# 新增模型

所有现有模型已迁移到公共模板，可直接参考它们的 model.json。
标准模型只需新增配置，不需要复制推理或缓存代码，也不需要修改编排器分支。

## 生成目录

从仓库根目录执行，将仓库、版本和镜像替换为真实值：

```powershell
uv run python scripts/new_model.py --directory 006-example-image --service example --engine sglang --kind image --model org/model --revision <模型commit> --image <镜像地址及digest>
```

生成 model.json、app.py、download_model.py、run.py、task.example.json、pyproject.toml 和 README.md。
生成器读取 [配置模板](../templates/standard-model/model.json)，拒绝覆盖已有目录。
模板的占位值不能直接部署。

主要编辑 model.json：

| 字段 | 内容 |
| --- | --- |
| service / operation / kind | 服务 ID、默认能力、image 或 video |
| model / revision | 必填：仓库与版本；revision 支持 commit、分支（如 main）或标签，推荐固定完整 commit |
| image / build_commands | 引擎镜像和可选镜像构建命令 |
| command | 引擎 argv，支持 {model}、{model_path}、{port}、{encoder_path} 等组件占位符 |
| defaults / options | 选填，省略分别等于 {}：默认请求参数、允许调整的参数与校验规则 |
| operations | 多能力的输入与默认值覆盖；参考 Pika t2va/fl2va |
| components | 额外权重组件；参考 MiniMax encoder |
| cache | hf 或 sglang_overlay；可设置下载模式与必需文件 |
| proxy_port / frame_transport | 可选：公共网关端口（默认 port + 1）；keyframes 用于视频首尾帧传输 |
| validator | 可选跨字段校验器 shared/params_<名称>.py |
| gpu / cpu / memory | Modal 资源配置 |

未知生成参数拒绝。普通参数的 type、minimum、maximum、enum、pattern 使用配置表达；
确实需要跨字段约束时，才添加 params_*.py，其 validate(params) 只检查参数，不再定义默认值。

省略 options 表示不允许 task 覆盖模型参数；省略 defaults 表示使用引擎默认值，
仍需确保引擎返回公共客户端要求的格式（图片为 Base64）。显式填写 null 或数组不会被当成空对象。
revision 不允许空白字符或未替换的 REPLACE_ 占位值；引用是否存在由 CPU 下载器确认，
离线校验不会联网查询。额外 components 的 revision 使用相同规则。
port 和 gpu 也可省略，默认分别为 8000 和 RTX-PRO-6000。
图片和视频自动使用相同的生成准入规则，新增模型无需编写并发锁或任务恢复策略。
引擎端口仅供容器内部使用，Modal HTTP 和 SDK 都访问网关端口。
异步视频协议必须返回 id，并提供 completed / failed / cancelled 终态；查询失败或未知状态不会释放生成名额。

## 使用

运行目录均为仓库根目录：

```powershell
uv sync --project 006-example-image
uv run python 006-example-image/run.py --task 006-example-image/task.example.json --dry-run
uv run python -m modal run 006-example-image/download_model.py
uv run python -m modal deploy 006-example-image/app.py
uv run python 006-example-image/run.py --task 006-example-image/task.example.json
```

日常业务 task 保存到根目录 tasks/，素材放 inputs/，输出放 outputs/。
三个入口通常无需修改。CPU 下载器 --check-only 校验缓存；HF 权重 --refresh 显式重新下载。
HF 分支引用在首次准备时解析并固定到缓存 snapshot，GPU 不联网补权重。
CPU SGLang Overlay 物化和 GPU 加载必须使用同一引擎镜像，不能用普通 snapshot 下载替代 Overlay。

## 接入 HTTP 工作流

编排器自动发现一级模型目录的 model.json，注册 service.operation。
为新模型添加 workflows/<service>-<operation>.json，步骤 uses 填对应能力。
服务地址与代理鉴权环境变量放在 endpoints.local.json（格式参考 endpoints.example.json），
然后使用 orchestration plan/run/resume。单模型 Task 的 params 与 seed 规则和 SDK 一致；
多步骤组合的参数写在各 workflow step 中。

## 验收

先执行离线参数预览、测试和 Modal 本地注册检查，再准备权重、Deploy 和做单条真实推理。
检查首尾帧传输、输出校验、网络中断恢复及模型实际支持的参数。
通用模板复用生命周期与标准协议；新引擎的命令、模型依赖和实际显存仍需分别验收。
