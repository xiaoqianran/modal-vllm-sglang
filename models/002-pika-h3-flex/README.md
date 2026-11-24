# Pika H3 Flex

SGLang Overlay；固定 6 步，5–15 秒，支持 t2va 与 fl2va。 模型参数、资源与启动命令统一写在 model.json。
模型目录保留 app.py 和 download_model.py 两个操作入口，Pika 专用实现位于 model_impl/；业务任务统一交给 orchestration。

从仓库根目录执行：

```powershell
# 离线校验
uv run python run.py plan tasks/004-pika-video-demo.json
# 免费预览下载计划：不会启动 Modal
uv run python models/002-pika-h3-flex/download_model.py
# 经确认后启动轻量 CPU 权重下载；随后单独 CPU 物化，默认 4 Worker
uv run python models/002-pika-h3-flex/download_model.py --execute
uv run python -m modal deploy models/002-pika-h3-flex/app.py
# 此自检会启动 GPU，但不生成媒体
uv run python run.py check-deployment pika
# 终端一保持 worker 运行
uv run python run.py worker
# 终端二登记任务，立即返回；以后还可随时追加其他 Task
uv run python run.py run tasks/004-pika-video-demo.json
uv run python run.py status outputs/004-pika-video-demo/001-main
uv run python run.py resume outputs/004-pika-video-demo/001-main
```

run 没有同步/异步开关，统一异步登记；worker 才提交实际 GPU 推理。
同模型的 SDK 推理串行，不同模型容器可重叠工作。每个模型最多一个 GPU 容器，空闲窗口 30 秒。
结果先写入 modal-vllm-sglang Volume，再独立下载到 outputs/<project>/<scene>/。
同模型排队通常复用热容器，不能保证容器永不回收或多客户端严格 FIFO。

首尾帧组合参考 [Task 006](../tasks/006-moss-pika-demo.json)，图片不经本地电脑回传给视频模型。
新 Volume 不会自动包含旧盘权重；空盘使用普通准备命令，同盘旧清单才使用 --migrate-only。
CPU 下载器 --check-only 检查缓存，--refresh 显式重新准备；三种准备模式互斥。
真实生成、GPU 快照恢复和性能仍需部署后验收。
详见 [统一工作流说明](../../docs/workflow-runner.md) 与 [根目录说明](../../README.md)。

统一入口由 modal-benchmark 下载固定版本的 Pika Adapter、Overlay 元数据和 MiniMax-H3 基础组件，再由 SGLang 物化本地目录；不能跳过 Overlay 物化直接推理。Pika 的 download_model.py 已改为此入口的本地 CLI 包装器，不再使用旧版单容器 modal run 下载。
使用 `--execute --check-only` 检查缓存；使用 `--execute --migrate-only` 转换同盘已有的旧清单。
引擎端口为 30010，ZMQ Broker 为 30011，内部网关为 30020。GPU 默认使用 Modal 原生镜像和固定 SHA-256 的官方 SGLang Nightly wheel，已通过 864x480 文生视频及音频、文件完整性验收；首尾帧、重新物化及 GPU Snapshot 恢复仍需单独验收。
