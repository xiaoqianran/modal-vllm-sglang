# Pika H3 Flex · 唯一 GPU 推理主流程

**CPU 模型准备 → `modal deploy app.py` → `run.py` 指定 JSON。**

## 简化后的源码职责

| 文件 | 职责 |
| --- | --- |
| `app.py` | 唯一的 `Pika` GPU Class，启动一次 SGLang 和 HTTP 代理；SDK 和 HTTP 共享同一容器 |
| `video.py` | T2VA / FL2VA 参数、输入哈希、一次视频生成、SHA256 校验、断点恢复 |
| `run.py` | 本地 JSON CLI；已登录 Modal 后查找部署的 `Pika.generate_batch` |
| `proxy.py` | 仅处理 HTTP 请求转发与 FL2VA Base64 首尾帧落盘，供跨模型 Workflow 使用 |
| `download_model.py` / `model_cache.py` | CPU 预检、权重物化和 GPU 加载前缓存校验 |

删除了旧的 `run_batch.py`、`batch_run.py`、`generate.py` 和只为单视频使用的请求示例；单视频直接用 `--limit 1`。

## 资源和接口

- 模型：`Pika-Labs/Pika-H3-Flex`；GPU：`RTX-PRO-6000`；4 CPU / 48 GiB RAM
- 容器：Class 级 `min_containers=0`、`max_containers=1`、`scaledown_window=30` 秒
- 推理：SGLang Diffusion，固定六步，`performance=memory`、offload `text_encoder,vae`、resident `video_vae=36`
- 同一个 SGLang 端口 `30010` 同时供 SDK 批量和 HTTP 代理 `30011` 调用
- HTTP：保留 `/v1/videos` 及轮询/下载接口，`requires_proxy_auth=True`；SDK：使用本机 Modal 登录凭证，不需要 HTTP URL/Token
- Class 允许 HTTP 与 SDK 同时进入同一容器；SDK 批量调用内部串行。SGLang 对同时提交的 HTTP 视频任务仍负责队列调度，**真实 GPU 并发、排队、显存压力需要验收**，不要把单容器误认为单任务锁。

## 准备与部署

在 `002-pika-h3-flex` 目录：

```powershell
uv run python -m modal run download_model.py --preflight-only
# 缓存未准备或校验失败时才执行：
uv run python -m modal run download_model.py
# 准备好后只需 Deploy 一次；代码变更后重新 Deploy：
uv run python -m modal deploy app.py
```

CPU 过程只生成现有 `modal-forge` Volume 下的 `009-pika-h3-flex` 缓存与 `model-ready.json`，不运行 GPU 推理。GPU 运行时禁止联网补权重。

## 指定 JSON 运行

从仓库根目录：

```powershell
# 文本生视频，先尝试一个镜头
uv run python 002-pika-h3-flex/run.py --workflow workflow_t2va.json --task tasks/t2va_demo.json --limit 1

# 首尾帧生视频
uv run python 002-pika-h3-flex/run.py --workflow workflow_fl2va.json --task tasks/fl2va_demo.json
```

本地入口通过 `modal.Cls.from_name("modal-forge-009-pika-h3-flex-fl2va", "Pika")()` 调用 `generate_batch`，不会创建第二个 GPU App。前后帧按 SHA256 上传 Volume，结果验证哈希后下载到 `outputs/<project>/<scene>/`。

默认 `run_id` 由本次任务输入生成，重复运行相同任务可找到原远端路径并复用已完成片段；需要恢复旧版随机批次时手动提供 `--run-id <原 ID>`。

**不可覆盖**：已有本地/云端视频只在输入签名、模型清单及 SHA256 均匹配时复用；否则拒绝覆盖。旧版视频元数据的哈希口径与新版不同，遇到旧文件校验失败请先备份并人工核对，不能直接用新脚本覆盖。

## 验证边界

目前只完成本地代码、模拟 SGLang API 的测试和 Modal SDK 注册检查。**没有重新 Deploy，也没有进行真实付费 GPU 生成验收**。默认不启用 GPU Snapshot，Deploy 本身不能消除模型加载冷启动。历史 SGLang 兼容性资料见 [验证记录](docs/2026-11-08-pika-h3-flex-validation.md)。
