# MiniMax H3 Image — Deploy 后按 JSON 生图

现在只有 **一个 `H3` GPU Class**：`@modal.enter` 启动一次 SGLang，私有 HTTP `serve` 与 SDK `generate_batch` 共享同一个 GPU 容器池、模型和权重缓存。

## 简单的文件结构

| 文件 | 唯一职责 |
| --- | --- |
| `app.py` | Modal Class、启动/退出、私有 HTTP、可选 GPU Snapshot |
| `image.py` | 文生图参数和签名、调用运行中的 SGLang、写入并校验 Volume 图片 |
| `run.py` | 本地只传 JSON，通过 `modal.Cls.from_name(..., "H3")` 调用、下载和恢复 |
| `model_cache.py` | 模型、编码器及 SGLang revision；GPU 启动前严格验证缓存 |
| `download_model.py` | CPU 预先下载和缓存检查，不启动 GPU |

MiniMax 模型 `MiniMaxAI/MiniMax-H3-Image`，单张默认 **2048 × 2048、6 步**；只支持文生图，不把 H3 文本模型的全部权重下载到 GPU。允许 32 倍数对齐、4–20 MP 的图片尺寸，参数验证同时用于 HTTP 和 SDK。

## 运行

首次准备（在 `005-minimax-h3-image` 目录）：

```powershell
uv run python -m modal run download_model.py --check-only
# 缓存还不存在时才需要：uv run python -m modal run download_model.py
uv run python -m modal deploy app.py
```

之后从仓库根目录直接执行（无 HTTP URL/Token）：

```powershell
uv run python 005-minimax-h3-image/run.py --task tasks/005-minimax-image-demo.json --dry-run
uv run python 005-minimax-h3-image/run.py --task tasks/005-minimax-image-demo.json
```

默认输出 `outputs/005-minimax-image-demo/001-main/001.png`；一个 JSON 里增加 `items` 就能批量生成。可以在任务顶层或某个 item 中添加 `"params": {"size": "4096x4096"}`，不改变 `num_inference_steps=6`。

同一任务重复执行时先校验本地图片，网络中断时优先通过 Volume 恢复已提交结果，**只有远端不存在的图片才会调用 GPU**。历史文件哈希或请求参数不匹配时拒绝覆盖；重用不同版本应换任务名或输出路径。

## GPU 与私有 HTTP

- GPU：**RTX PRO 6000**；12 vCPU、256 GiB RAM
- `min_containers=0`、`max_containers=1`、空闲约 30 秒缩容
- `@modal.web_server(..., requires_proxy_auth=True)` 仍保留，供 `python -m orchestration` 的跨模型工作流使用
- HTTP 仍需要 `MINIMAX_ENDPOINT`、`MINIMAX_MODAL_KEY`、`MINIMAX_MODAL_SECRET`；只有 SDK `run.py` 不需要
- SGLang 源码和模型 revision 已固定；仍复用 Volume `modal-forge` 中旧 `/mnt/modal/003-minimax-h3-image` 路径，**避免重新下载约 135 GiB 模型**。

GPU Memory Snapshot 默认关闭。需在 Deploy 前显式开启实验：

```powershell
$env:MINIMAX_GPU_SNAPSHOT = "1"
uv run python -m modal deploy app.py
```

Snapshot 属于实验功能，不保证显著提速，也不自动复用每次模型版本升级前的旧快照。关闭开关进行普通 Deploy 可以回退。Deploy 后仍以实际 URL 检查私有 HTTP 地址。

**验收边界**：当前 SDK Class、参数、离线图像写入和断点恢复已有本地测试；还没有真实付费 GPU 的 MiniMax 生图、显存峰值、快照恢复和 HTTP+SDK 并发测试。这些必须在部署后单独验证。

官方参考：[MiniMax-H3-Image](https://huggingface.co/MiniMaxAI/MiniMax-H3-Image)、[SGLang Recipe](https://github.com/sgl-project/sglang/blob/main/docs/cookbook/diffusion/MiniMax/MiniMax-H3-Image.mdx)。
