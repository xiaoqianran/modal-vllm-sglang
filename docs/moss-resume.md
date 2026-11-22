# MOSS 统一恢复

MOSS 与视频、组合任务都使用 orchestration。

```powershell
uv run python run.py plan tasks/003-stop-rain-001-rain-bus-stop.json
uv run python run.py run tasks/003-stop-rain-001-rain-bus-stop.json
# 另一个终端保持运行
uv run python run.py worker
uv run python run.py status outputs/003-stop-rain-001-rain-bus-stop/001-main
uv run python run.py resume outputs/003-stop-rain-001-rain-bus-stop/001-main
```

run/resume 只登记后返回；worker 查询原云端调用，校验并补下载已有产物。
已完成工作不重新生成；提交结果不明且无法恢复时不自动重提。
勿删除 state.json 来强行重试。详见 [工作流说明](workflow-runner.md)。
