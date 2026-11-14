# modal-vllm-sglang

**唯一 GPU 推理流程：CPU 准备模型 → `modal deploy app.py` → 调用已部署函数。**
所有生图、生视频均通过持久部署的 Modal App；不再使用临时 `modal run` GPU 任务、`app.run()` 或 detached Session Worker。

## 三个服务

| 模型 | Modal App | GPU | 推理入口 |
| --- | --- | --- | --- |
| `001-moss-image` | `modal-forge-008-moss-image` | RTX PRO 6000 | 一个 GPU Class，共享 SDK 批量生图 + 私有 HTTP |
| `002-pika-h3-flex` | `modal-forge-009-pika-h3-flex-fl2va` | RTX PRO 6000 | 一个 GPU Class，共享 SDK 批量视频 + 私有 HTTP |
| `005-minimax-h3-image` | `modal-forge-005-minimax-h3-image` | RTX PRO 6000 | 一个 GPU Class，共享 SDK 批量生图 + 私有 HTTP |

三种服务均 `min_containers=0`、`max_containers=1`；无任务时无需常驻 GPU。HTTP 接口始终要求 Modal Proxy Auth。**本地 SDK 通过已登录的 Modal 账号鉴权，不需要 HTTP URL、Modal-Key 或 Modal-Secret。**

## Python 依赖与 uv

根目录及每个模型目录是独立的本地 Python 项目：各自的 `pyproject.toml` 声明依赖范围，各自的 `uv.lock` 固定解析后的版本。**不要把 `uv.lock` 加入 `.gitignore`**，它应与对应的 `pyproject.toml` 一起提交。

- 在仓库根目录执行 `uv sync`：读取根目录 `pyproject.toml` 和 `uv.lock`，准备本地编排器/SDK 环境。
- 在模型目录执行 `uv sync`：读取该模型自己的 `pyproject.toml` 和 `uv.lock`，准备该模型的本地 Modal CLI 环境。
- 从任意目录明确选择项目：`uv sync --project 001-moss-image`。
- CI 或希望严格验证提交依赖：`uv sync --locked`；如果 lock 与 pyproject 不一致就报错，不会静默改写。

`pyproject.toml` 是依赖声明来源；有有效 `uv.lock` 时，`uv sync` 按 lock 安装精确版本。缺少或过期的 lock 会被重新解析并更新。云端 GPU 镜像仍由各自 `app.py` 构建，不能把本地 Modal CLI 环境等同于 GPU 镜像依赖。

MOSS 现已从原来两个独立 GPU 函数改为一个 `Moss` Class，避免同时调用 HTTP 和 SDK 时分别启动 RTX PRO 6000。**只修改了 PC2 本地代码，云端仍在执行的旧版任务不会自动改变。任务完成后才应重新 Deploy**；这个 Class 改造仍需真实 GPU 验证。

## 1. 准备缓存（仅 CPU，不是推理）

只在首次准备权重或缓存损坏时执行。以下是每个目录内的命令：

```powershell
cd 001-moss-image
uv run python -m modal run download_model.py --check-only
# 若模型还未准备：uv run python -m modal run download_model.py

cd ../002-pika-h3-flex
uv run python -m modal run download_model.py --preflight-only
# 若模型还未准备：uv run python -m modal run download_model.py

cd ../005-minimax-h3-image
uv run python -m modal run download_model.py --check-only
# 若模型还未准备：uv run python -m modal run download_model.py
```

这些 `modal run download_model.py` 是 CPU 缓存准备工具，不创建 GPU 推理容器。已有 `modal-forge` Volume 和历史权重路径保持不变。

## 2. 每个模型只 Deploy 一次

在相应模型目录执行：

```powershell
uv run python -m modal deploy app.py
```

Windows 上 `uv run modal` 可能触发 trampoline 路径错误，使用 `uv run python -m modal`。修改 `app.py` 或部署内函数后应重新 Deploy；普通任务无需重复部署。

## 3. 调用 MOSS：只提供 JSON

从仓库根目录执行，无需配置 HTTP 凭证：

**运行 003 之前**，旧 `outputs/.sessions` 中已有两条已完成但未下载到本地的分镜记录。先执行 `uv run python scripts/recover_legacy_sessions.py --fetch` 下载可验证的历史结果（只读 Volume，不启动 GPU），避免重复付费生成。

```powershell
uv run python 001-moss-image/run.py `
  --task tasks/003-stop-rain-001-rain-bus-stop.json `
  --task tasks/003-stop-rain-002-bridge-underpass.json `
  --task tasks/003-stop-rain-003-glass-greenhouse.json `
  --task tasks/003-stop-rain-004-after-rain.json
```

以上四场次共 26 张图，一次 SDK 批量调用；在末尾加 `--dry-run` 可离线预览。重复同一命令会核查既有文件与 SHA256，继续生成缺失图片，绝不直接覆盖已有成果。结果写入 `outputs/003-stop-rain/<场次>/`。见 [MOSS 断点续跑](docs/moss-resume.md)。

## 4. 调用 Pika：已部署 SDK 批量视频

从仓库根目录执行（无需 HTTP 凭证）：

```powershell
uv run python 002-pika-h3-flex/run.py --workflow workflow_t2va.json --task tasks/t2va_demo.json --limit 1
```

首尾帧生视频改为 `--workflow workflow_fl2va.json --task tasks/fl2va_demo.json`。该脚本通过部署的 `Pika` Class 调用 `generate_batch`，默认按任务内容生成稳定的远端批次 ID，保留视频校验、Volume 输出与断点恢复。详见 [Pika 说明](002-pika-h3-flex/README.md)。

## 5. MiniMax H3：指定 JSON 直接生成

```powershell
uv run python 005-minimax-h3-image/run.py --task tasks/005-minimax-image-demo.json --dry-run
uv run python 005-minimax-h3-image/run.py --task tasks/005-minimax-image-demo.json
```

该 SDK 入口调用同一部署中的 `H3.generate_batch`，不需要 `MINIMAX_ENDPOINT` 或 Proxy Token。已验证的本地文件直接跳过；网络中断后先从 Volume 恢复已提交的图片，只有缺失图片才触发 GPU。需要修改尺寸时，在任务 JSON 顶层或 item 中使用 `"params":{"size":"4096x4096"}`。

## 6. HTTP 跨模型工作流

跨模型的 HTTP 编排仍由 `python -m orchestration plan/run/resume/status` 提供。HTTP 客户端需要对应的 `*_ENDPOINT` 和 `*_MODAL_KEY` / `*_MODAL_SECRET` 环境变量，三个模型的 SDK 批处理均不需要。见 [HTTP Workflow 说明](docs/workflow-runner.md)。

MiniMax H3 的 `MINIMAX_GPU_SNAPSHOT=1` 为**可选实验开关**；001/002 尚未接入 GPU Snapshot。**Deploy 不等于自动生成显存快照**，SGLang/vLLM 的冷启动性能仍需真实 GPU 测试。

## 7. 历史数据恢复

旧 detached Session 已停用，但不会删除 `outputs/.sessions/tasks.sqlite3` 或云端 `session-results/`。若要列出旧任务或只下载已生成图片（不调用 GPU）：

```powershell
uv run python scripts/recover_legacy_sessions.py
uv run python scripts/recover_legacy_sessions.py --fetch
```

`--fetch` 会检查结果 SHA256 并拒绝覆盖既有文件。原有目录和生成记录都需要保留。

新增模型遵循 [模型接入规则](docs/add-model.md)。修改代码不等于完成云端验收，本轮未执行有费 GPU 生成。
