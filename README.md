# modal-vllm-sglang

参考 modal-comfyui-caru-ini 的分层架构：保留项目独立入口，公共能力放在 shared/，将模型下载与 GPU 推理分离。

## 目录

    modal-vllm-sglang/
    ├── shared/
    │   ├── modal_setup.py       # Modal Volume、镜像及轻量代码挂载
    │   ├── runtime.py           # 健康检查、子进程监控和清理
    │   ├── cache.py             # 模型文件大小清单
    │   └── files.py             # 原子 JSON、哈希、Volume 下载
    ├── 001-moss-image/
    │   ├── download_model.py    # 独立 CPU 下载
    │   ├── model_cache.py       # GPU 只读校验
    │   ├── app.py               # vLLM 服务
    │   ├── batch_storyboard.py  # 批量生图
    │   └── benchmark.py         # 性能测试
    ├── 002-pika-h3-flex/
    │   ├── download_model.py    # CPU 下载及 SGLang Overlay 物化
    │   ├── model_cache.py       # GPU 只读校验
    │   ├── app.py               # SGLang 服务及 Pika 帧代理
    │   ├── run_batch.py         # Modal Run 批量视频
    │   └── batch_run.py         # HTTP 批量客户端
    └── tests/                    # 共享模块离线测试

## 设计约束

- 下载单独占用 CPU，不在 GPU 启动时下载模型。
- 继续复用现有 Modal Volume：名称 modal-forge；MOSS 对应 007-moss-image；Pika 对应 009-pika-h3-flex。
- 云端模型缓存路径和既有 Modal App 名称均不随本地目录名称变化。
- MOSS 的 vLLM、Pika 的 SGLang Overlay 和关键帧代理保持各自实现，不强行抽象成统一推理协议。
- 轻量代码在镜像最后以 copy=False 挂载，避免修改业务脚本触发大型镜像层重建。
- 仍从各子目录执行 Modal 命令；无需安装额外公共包。
- 本次不改变 GPU 类型、推理参数、API 认证或并发限制。

## 运行顺序

### MOSS（首次需 CPU 准备）

    cd 001-moss-image
    uv run modal run download_model.py
    uv run modal run download_model.py --check-only
    uv run modal deploy app.py

后续也可使用 uv run modal run benchmark.py 或 uv run modal run batch_storyboard.py。

旧缓存即使已经存在，也必须首次通过 CPU 入口生成 model-ready.json。HF 缓存会优先复用已有文件；实际是否需要下载额外权重由模型版本和远端缓存决定。CPU 与 GPU 使用同一份相对路径清单，GPU 设置 HF_HUB_OFFLINE=1 与 TRANSFORMERS_OFFLINE=1。

### Pika

    cd 002-pika-h3-flex
    uv run modal run download_model.py --preflight-only
    uv run modal run download_model.py
    uv run modal run run_batch.py --workflow workflow_t2va.json --task tasks/t2va_demo.json

按需运行 uv run modal deploy app.py。Pika 现有公开服务仍不要求 Proxy Token，注意控制 URL 的传播和费用风险。

## 测试

在仓库根目录：

    cd modal-vllm-sglang
    002-pika-h3-flex\.venv\Scripts\python.exe -m unittest discover -s tests -v

在 Pika 目录：

    cd 002-pika-h3-flex
    uv run python -m unittest discover -s tests -v

本次只进行代码和 CPU 离线测试。尚未执行新的 Modal CPU 下载任务或 GPU 推理，无法保证新部署环境端到端通过。修改本地文件不会自动更新已部署的 Modal 服务。
