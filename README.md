# modal-vllm-sglang

参考 modal-comfyui-caru-ini 的分层架构：保留项目独立入口，公共能力放在 shared/，将模型下载与 GPU 推理分离。

## 目录

    modal-vllm-sglang/
    ├── shared/
    │   ├── modal_setup.py       # Modal Volume、镜像及轻量代码挂载
    │   ├── runtime.py           # 健康检查、子进程监控和清理
    │   ├── cache.py             # 模型文件大小清单
    │   ├── files.py             # 原子 JSON、哈希、Volume 下载
    │   ├── http.py              # 安全重试与任务轮询
    │   ├── inference_api.py     # vLLM 图片 / SGLang 视频 API 客户端
    │   └── batch.py             # 输入签名、断点续跑、状态汇总
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


## 公共批处理与 HTTP 约定

- shared/http.py：只对只读 GET 允许有限重试。生成类 POST 强制单次提交，避免超时后重复扣费及生成重复任务。
- shared/batch.py：稳定的任务标识、输入签名、输出哈希和元数据匹配校验；所有条目使用相同的状态计数工具。
- MOSS 图片批处理改为检查输入签名及结果文件 SHA256。历史上只有 PNG、没有条目级 JSON 的结果**不会直接跳过**，同一输出前缀下首次执行将重新生成，以避免静默复用错误内容。
- Pika 的 HTTP 客户端、Modal Run、单视频请求统一使用 SGLangVideoClient，保留各自模型参数和视频任务路径。
- 不自动改变原有云端 GPU 配置、模型缓存、Modal 资源参数和既有服务鉴权。
- 修改后的 GPU 端到端验证仍需在 Modal 上执行，离线单测不能代替真实视频或图片推理。

## 推理 API 抽离（2026-11-08）

`shared/inference_api.py` 封装了两种不同的调用协议，底层继续使用 `shared/http.py`：

- `VLLMImageClient(endpoint).generate(payload)`：调用 `/v1/images/generations`，返回 `(image_bytes, ".png" / ".jpg")`，不负责 MOSS 的尺寸、步数、CFG 等业务参数。
- `SGLangVideoClient(endpoint)`：`submit(payload)` 提交一次并返回任务 ID、`wait(job_id)` 查询状态、`content(job_id)` 下载并验证 MP4；也可用 `generate(payload)` 顺序执行并返回 `(job_id, video_bytes)`。
- 生成类 POST **不自动重试**。只有状态查询和结果下载允许可控重试，避免响应丢失后重复提交计费任务。
- 保持两个独立推理引擎、各自模型配置、下载器、Modal App 和 GPU 参数，不引入统一的模型管理大类。
- `001-moss-image/batch_storyboard.py` 与 `benchmark.py` 调用图片客户端；`002-pika-h3-flex/batch_run.py`、`generate.py` 和 `run_batch.py` 调用视频客户端。
- `001-moss-image/benchmark_sglang.py` 是**独立的实验性 SGLang 图像基准**，使用单独锁定的镜像和依赖，暂不改变其运行配置及客户端。

使用示例（`payload` 保留各模型要求的完整参数）：

```python
from shared.inference_api import VLLMImageClient, SGLangVideoClient

image, extension = VLLMImageClient("http://127.0.0.1:8000").generate(image_payload)

video_client = SGLangVideoClient("http://127.0.0.1:30010")
job_id = video_client.submit(video_payload)
video_client.wait(job_id, timeout=3600, poll=2)
video = video_client.content(job_id)
```

验证：在仓库根目录执行共享测试与 Pika 测试；检查 Modal CLI 的入口解析。真实端到端 GPU 推理仍需单独验证。
