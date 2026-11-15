# MOSS Image

vLLM-Omni；默认 1024×1024、50 步。 模型参数、资源与启动命令统一写在 model.json。
模型目录只保留 app.py 和 download_model.py 两个操作入口，业务任务统一交给 orchestration。

从仓库根目录执行：

```powershell
# 离线校验
uv run python -m orchestration plan tasks/003-stop-rain-001-rain-bus-stop.json
# CPU 准备新 Volume 的权重，然后部署
uv run python -m modal run 001-moss-image/download_model.py
uv run python -m modal deploy 001-moss-image/app.py
# 此自检会启动 GPU，但不生成媒体
uv run python -m orchestration check-deployment moss
# 终端一保持 worker 运行
uv run python -m orchestration worker
# 终端二登记任务，立即返回；以后还可随时追加其他 Task
uv run python -m orchestration run tasks/003-stop-rain-001-rain-bus-stop.json
uv run python -m orchestration status outputs/003-stop-rain-001-rain-bus-stop/001-main
uv run python -m orchestration resume outputs/003-stop-rain-001-rain-bus-stop/001-main
```

run 没有同步/异步开关，统一异步登记；worker 才提交实际 GPU 推理。
同模型的 SDK 推理串行，不同模型容器可重叠工作。每个模型最多一个 GPU 容器，空闲窗口 30 秒。
结果先写入 modal-vllm-sglang Volume，再独立下载到 outputs/<project>/<scene>/。
同模型排队通常复用热容器，不能保证容器永不回收或多客户端严格 FIFO。

首尾帧组合参考 [Task 006](../tasks/006-moss-pika-demo.json)，图片不经本地电脑回传给视频模型。
新 Volume 不会自动包含旧盘权重；空盘使用普通准备命令，同盘旧清单才使用 --migrate-only。
CPU 下载器 --check-only 检查缓存，--refresh 显式重新准备；三种准备模式互斥。
真实生成、GPU 快照恢复和性能仍需部署后验收。
详见 [统一工作流说明](../docs/workflow-runner.md) 与 [根目录说明](../README.md)。
