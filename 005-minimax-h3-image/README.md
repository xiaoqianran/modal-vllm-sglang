# MiniMax H3 Image

本模型使用公共模板，模型能力为 minimax-t2i。使用 SGLang，独立下载 image 与 encoder 两个组件。固定 6 步，默认 2048×2048，尺寸约束由 shared/params_minimax.py 检查。

GPU 服务资源先按 4 vCPU、32 GiB RAM、RTX PRO 6000 配置进行验收，不再预留 12 vCPU、256 GiB。32 GiB 目前只是待验证配置，并非实测峰值：权重的主机侧加载、mmap、文本编码器内存和 SGLang 运行时可能使实际需求更高。若容器被 OOM 杀死，应先分析 CPU RAM 峰值、模型卸载策略与 GPU 显存余量，再决定是否增加内存。CPU 下载容器独立使用 4 vCPU / 8 GiB，不受本项修改影响。

- model.json：模型、镜像、启动命令、权重组件、资源、参数与能力配置。
- app.py / download_model.py / run.py：公共实现的薄入口。
- 公共实现：shared/model_service.py、shared/model_cache.py、shared/model_spec.py。
- 日常任务在根目录 tasks/，素材在 inputs/，结果在 outputs/<project>/<scene>/。

从仓库根目录执行：

```powershell
uv run python 005-minimax-h3-image/run.py --task tasks/005-minimax-image-demo.json --dry-run
uv run python -m modal run 005-minimax-h3-image/download_model.py
uv run python -m modal deploy 005-minimax-h3-image/app.py
uv run python 005-minimax-h3-image/run.py --task tasks/005-minimax-image-demo.json
```

下载器 --check-only 检查 CPU 缓存。模型保留部署名称 H3 Class，SDK 与私有 HTTP 共用引擎。
完整任务格式、恢复机制与验证说明见 [根目录说明](../README.md)。
本轮代码迁移没有触发云端部署或付费 GPU 验收。

## 异步任务和八路图片下载

公共异步任务客户端支持 `--enqueue`、`--status`、`--collect`，
也支持无需本地任务登记表的 `--collect --remote`。从仓库根目录执行：

```powershell
uv run --no-sync python 005-minimax-h3-image/run.py --task tasks/005-minimax-image-demo.json --enqueue --dry-run
# 此命令将提交付费 GPU 生成，需先完成缓存准备及云端部署
uv run --no-sync python 005-minimax-h3-image/run.py --task tasks/005-minimax-image-demo.json --enqueue
uv run --no-sync python 005-minimax-h3-image/run.py --status
uv run --no-sync python 005-minimax-h3-image/run.py --collect
uv run --no-sync python 005-minimax-h3-image/run.py --collect --remote --task tasks/005-minimax-image-demo.json
```

普通收集和直接按 Task 收集共用 `ThreadPoolExecutor(max_workers=8)`，多个
Task 同时指定时最多并发八张图片。每个服务的登记表独立保存为
`outputs/.runner/jobs/<service>.json`；校验身份、模型缓存和 SHA-256 后才写入结果。
收集命令不会启动 GPU，不修改推理参数。
实际 Volume 检查发现当前缺少 `/003-minimax-h3-image/model-ready.json`，
须先验证或迁移 CPU 模型缓存；截至本次修改，尚未完成真实图片下载验收。

同步 `--task` 的结果下载也复用八线程收集器。已有旧缓存优先执行
`uv run python -m modal run 005-minimax-h3-image/download_model.py --migrate-only`；
该模式仅校验、迁移，不下载权重。部署后可用
`uv run python 005-minimax-h3-image/run.py --check-deployment` 检查配置及异步接口，
此自检会启动 GPU、加载权重，但不生成媒体。容器输入并发 `max_inputs` 默认 1，
SDK 推理仍串行。性能日志字段及采样边界见 [根目录说明](../README.md#7-后续云端验收入口本次未执行)。
