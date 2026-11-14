# HTTP Workflow / Task

单模型日常任务可以直接使用各模型的 run.py；跨模型组合使用本机 HTTP 编排器。
两者共用 model.json 中的能力、默认参数和 params_*.py 校验器。

## 使用

```powershell
uv run python -m orchestration plan tasks/001-story-demo.json
```

plan 完全离线。实际运行前为涉及的模型设置以下环境变量：

```powershell
$env:MOSS_ENDPOINT = 'https://your-moss-service.modal.run'
$env:MOSS_MODAL_KEY = 'your-key'
$env:MOSS_MODAL_SECRET = 'your-secret'
$env:PIKA_ENDPOINT = 'https://your-pika-service.modal.run'
$env:PIKA_MODAL_KEY = 'your-key'
$env:PIKA_MODAL_SECRET = 'your-secret'
uv run python -m orchestration run tasks/001-story-demo.json
uv run python -m orchestration status outputs/001-story-demo/001-main
uv run python -m orchestration resume outputs/001-story-demo/001-main
```

私有 HTTP 地址取自实际 Deploy 输出。不同端点配置用 --endpoints endpoints.local.json。
服务地址、鉴权和部署 revision 定义见 endpoints.example.json；部署配置变化时更新 revision。

## 配置

Workflow 定义步骤和素材连接，Task 定义本次提示词与镜头。能力从模型目录 model.json 自动发现：
目前有 moss.t2i、minimax.t2i、pika.t2va、pika.fl2va。
绑定只支持 input.FIELD 或 STEP.OUTPUT，输出名为 image / video，根据引用推导依赖。
首尾帧用 {"file":"inputs/pika/001_first.png"} 引入；文件在冻结计划时复制到运行目录。

单步骤 workflow 支持 task 顶层及 item 的 params，覆盖顺序为 step → task → item；
item 的显式 seed 优先，省略时按任务名、能力、镜头 ID 稳定生成，与 SDK 规则相同。
多步骤组合的参数写在对应 workflow step 中，seed 可以显式绑定 input 中的字段。
参见 tasks/001-story-demo.json 与 workflows/storyboard-to-video.json。

任务名与文件名一致，使用 NNN-short-description，project 默认 name，scene 默认 001-main。
输出直接写入 outputs/<project>/<scene>/；plan.json 保存冻结计划，state.json 是状态权威，
report.json 是摘要，.runner 保存请求和产物记录。

## 执行与恢复

按阶段串行执行，例如所有首帧 → 所有尾帧 → 所有视频。单 item 失败阻断其下游，其他 item 继续。

SDK 和 HTTP 工作流共用恢复判断：已有有效产物直接复用；有 job_id 则查询原任务；
提交后缺少 job_id 且没有有效产物时标记 uncertain，不自动重新生成；远端明确失败则停止。
同一模型的生成请求统一经过网关，视频直到终态才释放名额，GET 查询与下载无需等待生成锁。
网关明确拒绝且未转发的请求保持 pending，稍后使用原命令或 resume 重试。
提交结果不明确会关闭后续生成准入；需核查远端后重启整个模型容器，禁止单独重启网关绕过占用状态。
恢复读取冻结计划，不读取后续修改的 task/workflow；同一场次只允许追加新镜头。
文件有 SHA256 校验、原子写入与本机单写者锁。修改已有镜头应创建新场次。

生成 POST 不自动重试。请求前保存 submitting，拿到视频 ID 后先保存再轮询。
查询或下载失败可以 resume 同一个 job；没有 job ID 的不确定提交不会自动重新生成。
图片为同步协议，响应丢失时不能靠 HTTP 端点自动找回；需要逐张提交到 Volume 的恢复使用 SDK。
Ctrl+C 不代表云端任务取消。服务进程重启后视频 ID 可能失效，本地状态不能保证远端恰好执行一次。

## 验证

```powershell
uv run python -m unittest discover -s tests -v
```

全部测试在本地模拟执行。本轮未执行真实云端 GPU 验收。
