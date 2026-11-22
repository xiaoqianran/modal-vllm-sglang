# Pika H3 Flex — 三阶段运行目录

模型专用实现统一命名为 `model_impl/`；`model.json` 的
`cache_implementation` 明确指定该包，由公共下载器按需加载。

本项目继续沿用 `models/002-pika-h3-flex/model.json`，**不迁移旧模型目录或 Modal Volume**。

1. **Download** — `tools/download_models.py` 调用 `modal-benchmark`（轻量
   Debian 下载器、HF Xet）。下载文件存入 Volume
   `models:/models/002-pika-h3-flex/downloads/`，保留固定版本的三套
   HF 文件快照，已有数据可由下载器核验后复用。
2. **Materialize** — `models/002-pika-h3-flex/model_impl/materializer_image.py` 定义 Debian
   Slim + uv 的独立 CPU 镜像，`models/002-pika-h3-flex/model_impl/overlay.py` 检查固定注册表、
   本地快照及物化完成标记。物化结果写到
   `/data/models/002-pika-h3-flex/sglang/materialized_models/`；通过全量文件清单验证后，
   才更新 `/data/models/002-pika-h3-flex/model-ready.json`。
3. **Inference** — `runtime/serving/model_runtime.py` 读取上述已验证本地路径，
   GPU 推理继续使用 `model.json` 中固定的 SGLang 引擎镜像。不会为下载启动 GPU。

CPU 物化环境仍需 SGLang 的 Python 扩散依赖。**第一次 uv 安装并不一定比拉取
官方镜像快**；只有通过真实 CPU 云端构建和官方 Overlay 完整物化后，才能断言
部署可用。避免同时运行两个针对相同 Volume 的 Pika 写入任务。

免费检查：`uv run python models/002-pika-h3-flex/download_model.py`
和 `uv run --with pytest python -m unittest discover -s tests -q`。

完整下载与物化会使用付费 CPU 容器，只有明确授权后才运行
`uv run python models/002-pika-h3-flex/download_model.py --execute`。
旧的 `modal run models/002-pika-h3-flex/download_model.py` 已不再是这个模型的入口。
检查远端已准备缓存：`uv run python models/002-pika-h3-flex/download_model.py --execute --check-only`。
迁移现有同盘旧清单：`uv run python models/002-pika-h3-flex/download_model.py --execute --migrate-only`。
这两种模式都不允许下载，且需要明确的 `--execute` 来启动付费 CPU 检查容器。
