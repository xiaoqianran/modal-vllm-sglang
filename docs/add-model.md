# 新增模型：统一 Deploy 主流程

1. 新建独立模型目录，包含 `app.py`、`download_model.py`、`model_cache.py`、`pyproject.toml` 和 `README.md`。
2. CPU 侧准备并校验模型缓存；GPU 侧只读、离线加载，不下载权重。锁定镜像与模型版本，避免无意义地重新拉取大权重。
3. **所有 GPU 函数只注册到该模型的一个持久 Modal App**，以 `uv run python -m modal deploy app.py` 发布。不得新增通过 `modal run`、`app.run()` 或 detached Worker 启动的 GPU 推理入口。
4. 本地 CLI 使用 `modal.Function.from_name(...).remote(...)`、`remote_gen(...)` 或对应 `modal.Cls.from_name` 连接已部署函数。仅对跨模型 HTTP 工作流使用 `endpoints.example.json` 和 Proxy Auth。
5. 新增能力在 `orchestration/adapters.py` 声明输入与输出；可复用 `shared/inference_api.py` 时不要复制 HTTP 客户端。
6. 任务用 `tasks/`、`workflows/` 的 JSON 表达；输出按 `outputs/<project>/<scene>/` 保存。必须保留结果校验、内容哈希、不覆盖旧文件和断点续跑。
7. 先跑离线测试、参数预览和 SDK 函数注册检查，再分别验证部署、一次真实推理、恢复与费用。重要配置变化要更新 service revision。

现有样例：MOSS `001-moss-image`、Pika `002-pika-h3-flex`、MiniMax `005-minimax-h3-image` 均将私有 HTTP 与 SDK 批量方法注册在各自同一个 GPU Class，以 Class 级 `max_containers=1` 避免双容器。三个目录均采用 `app.py`（唯一 GPU 生命周期）、`image.py` 或 `video.py`（推理核心）、`run.py`（本地任务入口）。

**性能约束：Deploy 仅提供持久部署，不保证显存加载被快照。** GPU Snapshot 必须独立配置和验收。当前只在 MiniMax H3 提供实验开关，未验证 SGLang Diffusion 是否可可靠恢复。

历史 detached Session、临时 GPU Benchmark 和旧 `modal run` 批处理代码已淘汰。历史 Session 结果可以用 `scripts/recover_legacy_sessions.py` 只读恢复，绝不删除 `outputs/.sessions` 或既有 Volume 文件。
