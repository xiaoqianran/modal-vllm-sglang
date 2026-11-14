# 轻量 Workflow / Task 执行器

已实现的第一版：MOSS 生图、Pika 文生视频、MOSS 首尾帧 → Pika 视频。**本页说明的是可选 HTTP 工作流**：本机 CPU 编排器调用已部署的私有 HTTP 服务，需配置服务地址和 Proxy Auth 凭证。若只需单模型批量推理，先 Deploy 再使用 `001-moss-image/run.py`、`002-pika-h3-flex/run.py` 或 `005-minimax-h3-image/run.py`；三者均使用已登录 Modal SDK 查找部署中的 Class，无需 HTTP URL / Proxy Token。旧的临时 GPU App 入口已移除。

## 使用

在仓库根目录执行。先离线预览，不需要端点或密钥，不会访问服务或启动 GPU：

```powershell
uv run python -m orchestration plan tasks/001-story-demo.json
# 等价入口
uv run python -m orchestration run tasks/001-story-demo.json --dry-run
```

预览展示最终参数、自动 seed、引用关系与生成调用次数，不估算未经测量的 GPU 费用。

实际运行前，在当前终端设置服务地址与 MOSS、Pika 代理鉴权：

```powershell
$env:MOSS_ENDPOINT = 'https://your-moss-service.modal.run'
$env:PIKA_ENDPOINT = 'https://your-pika-service.modal.run'
$env:MOSS_MODAL_KEY = '<你的 Modal 代理 key>'
$env:MOSS_MODAL_SECRET = '<你的 Modal 代理 secret>'
$env:PIKA_MODAL_KEY = '<你的 Modal 代理 key>'
$env:PIKA_MODAL_SECRET = '<你的 Modal 代理 secret>'
uv run python -m orchestration run tasks/001-story-demo.json
```

以上 run 会调用真实 GPU 服务。新版运行目录固定为 `outputs/<project>/<scene>/`，**不再创建 run-id 子目录**。已有同一任务会复用该场次的状态；仅追加新的镜头 ID 时可继续执行，已完成镜头不重复提交。

本次 Pika HTTP 鉴权升级和 MiniMax Class 改造已更新 `endpoints.example.json` 的服务 revision。旧场次的冻结计划不会被静默覆盖；若原场次使用旧版服务且恢复校验不通过，应保留旧输出并使用新场次，而不是手动篡改状态文件。

```powershell
uv run python -m orchestration status outputs/001-story-demo/001-main
uv run python -m orchestration resume outputs/001-story-demo/001-main
```

`status` 显示已记录状态；`resume` 会重新验证产物哈希，并恢复可查询的任务。`--timeout 3600 --poll 2` 可用于 run/resume，分别设置生成提交超时、远端轮询预算（各自独立）和查询间隔。未全部完成时退出码为 1；配置错误为 2。

若服务配置不同，复制 `endpoints.example.json` 为已被 Git 忽略的 `endpoints.local.json`，运行时传 `--endpoints endpoints.local.json`。配置只填写环境变量名称，不能直接填写 token。当前 MOSS、Pika、MiniMax 示例均强制 Proxy Auth，须为涉及的模型设置有效凭证。执行器不修改云端鉴权设置；代码修改后需要重新部署才会在云端生效。

## 两份用户配置

Workflow 定义怎么做，Task 定义这次做什么。运行记录由程序生成，无需手写。

```text
shared/                  现有 HTTP、推理客户端等基础工具
orchestration/
  runner.py              校验、计划、执行与 CLI
  adapters.py            模型参数校验和协议适配
  state.py               文件锁、状态保存和产物校验
workflows/               可复用步骤
tasks/                   提示词、素材与镜头列表
outputs/<project>/<scene>/ 图片和视频直接存放；plan/state/report 及 .runner/ 管理状态
```

现有模板：`moss-t2i`、`pika-t2va`、`storyboard-to-video`、`minimax-t2i`。Task 的 `workflow` 填模板 ID，解析到 `workflows/<id>.json`。可选 `project` 和 `scene` 指定输出剧目与场次，未指定时默认 `project=name`、`scene=001-main`。

单模型任务示例，保存为 `tasks/002-image-demo.json`：

```json
{
  "version": 1,
  "name": "002-image-demo",
  "workflow": "moss-t2i",
  "items": [{"id": "001", "prompt": "A red paper boat at sunrise"}]
}
```

组合示例直接参考 `tasks/001-story-demo.json` 和 `workflows/storyboard-to-video.json`。首尾帧是独立生成的图片；数据流接通不等于保证角色或构图一致性。

约定：

- 新 Task 名为 `NNN-short-description`，必须与文件名一致；item ID 在 Task 内唯一，大小写冲突也会拒绝。
- `uses` 支持 `moss.t2i`、`pika.t2va`、`pika.fl2va`、`minimax.t2i`。
- 输入绑定只有 `{"ref":"input.FIELD"}` 或 `{"ref":"STEP.OUTPUT"}`。根据引用推导依赖，不写重复的 depends_on。
- 图片输出名为 `image`，视频输出名为 `video`。引用只能指向同一个 item 的上游步骤。
- 图片输入字段可以从 item 读取 `{"file":"inputs/frame.png"}`。文件路径相对仓库根目录，不随终端位置变化；外部素材先复制到 inputs。文件会复制进 Run，后续修改原文件不影响恢复。
- Workflow 的 `params` 可设置允许调整的模型参数，未知参数直接拒绝。Pika 限制 6 steps、5–15 秒、duration 与 seconds 一致；MOSS/Pika 模型默认值在适配器中补齐。
- 单个 item 可写 `"reuse_existing": true`，用于**明确采用**已经迁移的现有图片/视频：检查其格式和 SHA256 并写入来源记录，不再次提交 GPU 请求；这不代表它由当前冻结的 seed 生成。默认 false，遇到同名文件会拒绝覆盖。
- 缺省 seed 按 Task 名、step ID、item ID 稳定生成。需要自定时，在 Workflow inputs 加 `"seed":{"ref":"input.seed"}`，并在每个 item 填整数 seed。
- 第一版不支持分支、循环、跨 item 引用和动态插件。
- 目录版本 3 使用 `outputs/<project>/<scene>/001.png`、`001-first.png`、`001-last.png`、`001.mp4`。从 `002-again-love/001-hotel-corridor` 到其他场次时，镜头编号可在新场次从 001 开始。

默认按步骤执行整批：所有首帧 → 所有尾帧 → 所有视频，每次仅一个生成请求在途。这样减少 MOSS 与 Pika 交替调用的次数；实际冷启动和费用取决于部署及请求间隔，不保证零冷启动。

## 中断与恢复

```text
pending → submitting → running（异步）→ collecting → completed
                         ↘ failed / uncertain
上游未完成 → blocked
```

- 请求前持久化 submitting；收到远端 job ID 后立即保存，再查询状态。
- 生成 POST 始终只提交一次。网络失败、响应丢失或提交后崩溃不会自动重提。
- 有 job ID 的 uncertain 可以用 resume 继续查询；查询或下载失败时保留 ID。本版执行器每次读请求单次尝试，失败后由 resume 恢复，不在循环中无限重试。
- 没有 job ID 的 uncertain 无法自动恢复；同步 MOSS 响应丢失也属于这一类。核查后需要重新生成时，明确审核后创建新的镜头任务；旧记录保留，不自动重跑。
- 远端明确 failed/cancelled 记为 failed，不自动重跑。
- 本地文件先写临时文件并 fsync，再原子替换；manifest 最后保存。若 manifest 已完成而状态写入中断，resume 验证后补齐 completed。
- 输出损坏不能复用。视频可尝试从原 job 重新下载；没有查询接口的图片保持 uncertain，不重新生图。
- 单个 item 失败只阻断其下游，其余 item 继续。
- 同一个输出根目录以及运行目录均有 OS 文件锁。第一版只承诺同一本机、同一输出根目录内串行；不要用不同输出根目录绕过限制共享同一低并发服务。
- Ctrl+C 停止本地执行，不等于取消云端任务。

`plan.json` 保存最终参数与输入绑定；`state.json` 是运行状态权威；`report.json` 是执行结束时生成的摘要。每步有 request 描述、产物文件及 artifacts manifest。请求描述不含 token 或 Base64 图片。

恢复使用冻结计划，不读取后来修改的 Task/Workflow。计划被改动或 endpoint 变化时拒绝恢复。部署者需维护服务 revision；即使 URL 不变，替换权重或影响结果的服务配置后也应更新 revision 并建立新的场次任务，不能以旧配置继续同一个场次。第一版无法远程自动证明部署身份。

SGLang 远端任务可能只保存在进程内存中。服务重启后查询不到 ID 时，保持 uncertain；本地记录不能提供 exactly-once 保证。

## 验证与边界

```powershell
uv run python -m unittest discover -s tests -v
uv run --project 002-pika-h3-flex python -m unittest discover -s 002-pika-h3-flex/tests -v
```

测试使用临时目录、模拟服务和故障注入，不会调用 Modal GPU。覆盖顺序执行、素材传递、稳定 seed、配置拒绝、锁、提交后中断、job ID 恢复、下载失败、manifest 恢复和损坏文件拒绝。

本次实现尚未进行真实 GPU 端到端验证。ComfyUI 适配器、跨 Run 缓存、远端持久任务网关和对象存储归档均未加入；现有视频客户端仍将单个视频读入内存，超大产物的流式下载留作后续扩展。

旧 Pika HTTP 批处理的签名已补入实际 seed。旧输出元数据的签名可能不再匹配，旧入口再次运行时可能重新生成；新编排器使用独立 outputs 目录，不自动导入旧缓存。
