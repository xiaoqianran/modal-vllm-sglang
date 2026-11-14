# 统一模型服务升级

此升级保留三个 App 名称、原 HTTP endpoint 标签与 Moss / Pika / H3 部署类名。
SDK 方法采用新契约：`generate_batch_stream(items, prefix, expected_cache_fingerprint, expected_spec_hash)`。
旧 Pika / H3 的 `generate_batch` 及旧 Moss 的两参数调用不能直接继续使用；请切换各模型 `run.py --task ...`，或按公共实现的新签名调用。
Python 薄入口导出 `Model`，Modal 注册名由 `model.json` 的 `modal_class` 决定。
旧输出日志不迁移；新任务写入独立的 `model-results/`，不要用旧任务名重新生成未知状态的旧任务。

## 升级顺序

1. 暂停发起任务，确认旧任务已结束；保留原版本与旧任务日志。
2. 对三个模型分别执行 CPU 准备及检查：

```powershell
uv run python -m modal run 001-moss-image/download_model.py
uv run python -m modal run 001-moss-image/download_model.py --check-only
uv run python -m modal run 002-pika-h3-flex/download_model.py
uv run python -m modal run 002-pika-h3-flex/download_model.py --check-only
uv run python -m modal run 005-minimax-h3-image/download_model.py
uv run python -m modal run 005-minimax-h3-image/download_model.py --check-only
```

存在旧清单时原地校验并转换，权重路径不变；所有组件通过校验才发布新清单。
无缓存时普通准备会下载；已有缓存损坏时停止。`--refresh` 明确允许重新准备权重，不应用于普通格式迁移。
转换前的清单保存在同目录 `model-ready.legacy.json`。GPU 启动不执行迁移或下载。
校验是文件列表和大小校验，不能检测同长度的内容损坏。

3. CPU 检查成功后，分别 `uv run python -m modal deploy <模型目录>/app.py`。
4. 用新 run.py 或 HTTP 入口做每模型单任务验收，再恢复正常使用。

HTTP endpoint 标签保留旧值；部署后仍需从 Modal 输出核实实际 URL，核对环境变量 MOSS_ENDPOINT、PIKA_ENDPOINT、MINIMAX_ENDPOINT。
网关无跨 SDK/HTTP 的生成锁。SDK 原有批次锁保留；共享引擎决定并发处理方式。

## 恢复与回滚

提交前保存 submitting 并 commit；视频返回 ID 后保存 job_id 并 commit，再轮询。
提交超时且没有 ID 的任务停止自动重试；已有 ID 时继续查询同一任务。
下载或发布失败后保留 ID；临时文件采用独立文件名并清理本次文件。

若需回滚，先停止新任务和服务，再从 `model-ready.legacy.json` 恢复旧 `model-ready.json` 并提交 Volume，然后部署旧代码。
不要在新容器仍运行时切换清单。未改变的权重无需重新下载。

MOSS / Pika 仍沿用现有 main revision；准备成功后会记录实际快照和完整清单指纹，普通重跑复用缓存。
旧 Pika 清单没有可证明的提交版本，不能凭空指定“已验证”commit；固定引擎/模型版本需要实际环境验收后更新配置并重新准备。
本地离线测试不能代替 Volume 实际迁移与 Modal GPU 验收。
