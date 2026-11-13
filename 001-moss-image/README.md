# 001-moss-image

GPU 推理入口默认统一为 **4 核 CPU、8 GiB RAM**（8192 MiB），包括部署、批处理和基准脚本。此资源档位由用户确认已在远程 Modal 测试；基准脚本仍允许环境变量覆盖。

独立 Modal 生图服务：使用 vLLM-Omni 官方版本镜像；原有 007-video-understanding 不变。

## 模型及后端
MOSS-Image **同时支持 vLLM-Omni、SGLang 和 ComfyUI**。此模块实际实现 vLLM-Omni，因为官方有明确的 MOSS Docker 镜像、单 GPU 服务命令和兼容 OpenAI 的图像生成 API。SGLang 暂未加入部署实现，必须在相同环境实测后才能选择更快者。

默认模型 `OpenMOSS-Team/MOSS-Image-SFT`，可替换 `OpenMOSS-Team/MOSS-Image-Base`。默认 GPU `RTX-PRO-6000`。镜像 `vllm/vllm-omni:v0.32.0`，运行 `vllm serve MODEL --omni --enforce-eager`。已在 RTX PRO 6000 上成功加载模型并完成 50 步推理。使用 Modal Image Builder 2026.10 和 SDK 1.6.3，直接保留官方镜像的 Python 依赖；无需手动安装兼容包。

## 部署

```powershell
cd 001-moss-image
uv sync
$env:MOSS_MODEL = "OpenMOSS-Team/MOSS-Image-SFT"
$env:MOSS_GPU = "RTX-PRO-6000"
uv run modal run download_model.py
uv run modal run download_model.py --check-only
uv run modal deploy app.py
```

Modal App 名称：`modal-forge-008-moss-image`。使用现有 Modal Volume `modal-forge` 暂时复用历史 `007-moss-image` 子目录缓存模型（仅内部缓存路径不变，避免重新下载权重；每 60 秒尝试提交）；首次需先运行 CPU 下载入口生成完整缓存清单；GPU 端禁止联网补充权重。默认 `min_containers=0`、`max_containers=1`、每容器并发 1 个请求。空闲时不常驻 GPU。API 启用 Modal Proxy Auth；需要在 Workspace 创建 Proxy Token。

## 请求

服务路径 `POST /v1/images/generations`。使用 `Modal-Key` 和 `Modal-Secret` 请求头。示例 JSON：

```json
{
  "prompt": "A red fox in a snowy forest",
  "size": "1024x1024",
  "n": 1,
  "num_inference_steps": 50,
  "guidance_scale": 3.0,
  "flow_shift": 5.0,
  "seed": 0,
  "response_format": "b64_json",
  "extra_params": {
    "uncond_skip_layers": [22],
    "slg_start": 0.01,
    "slg_stop": 1.0
  }
}
```

## 性能问题

**vLLM-Omni 和 SGLang 不保证比 ComfyUI 快。** 同一模型、精度、分辨率、步数、CFG/SLG 和 GPU 才能比较。分别记录单张 warm 延迟、并发吞吐、显存峰值、冷启动与费用。Modal 多容器并行不等于同卡动态批处理。ComfyUI 如使用低步数 LoRA 或量化，不能和原版 BF16 50 步直接比较。

官方资料：
- https://github.com/OpenMOSS/MOSS-Image
- https://recipes.vllm.ai/OpenMOSS-Team/MOSS-Image-Base
- https://docs.vllm.ai/projects/vllm-omni/en/latest/models/supported_models/
- https://github.com/sgl-project/sglang

## Image Builder 2026.10 实测（2026-11-08）

- 已在 Modal 工作区启用 Image Builder 2026.10，PC2 Modal SDK 为 1.6.3。
- 删除了 app.py 和 benchmark.py 中旧版 Builder 所需的 4 项依赖强制安装补丁；只使用官方镜像 `vllm/vllm-omni:v0.32.0`。
- 真实 GPU 测试通过：RTX PRO 6000、MOSS-Image-SFT、1024×1024、50 步、HTTP 200。
- 本轮启动至服务就绪 40.03 秒；单张请求 14.88 秒；双请求分别 14.82 秒和 29.67 秒（请求排队而非同卡并行）。
- 测试运行：https://modal.com/apps/yuqueaaa/main/ap-EDds39wS99PEai8GvLWMA6
- 不再为了 Modal 本身修改 `typing_extensions`、`protobuf`、`aiohttp`、`idna`。如上游镜像后续升级，需重新验证。

## 已完成的实测（2026-11-08）

- 设备：Modal RTX PRO 6000（Blackwell）；模型：MOSS-Image-SFT；引擎：vLLM-Omni 0.32.0。
- 生成设置：1024×1024、50 步、CFG 3、flow shift 5、固定 seed、SLG skip layer 22。
- vLLM 日志中模型加载：14.90 秒、25.34 GiB（不是生成时间）。
- 50 步单图请求：引擎记录约 14.66 秒，HTTP 200；去噪吞吐约 3.46 step/s。
- 两个 HTTP 请求已完成（各返回 200）；引擎当前 max_num_seqs=1，请求排队，尚不能说明单卡支持双任务真正同步推理。
- 这是 vLLM 的单独基线，**尚无同条件 SGLang、ComfyUI 结果，不能排名**。
- Modal 任务：https://modal.com/apps/yuqueaaa/main/ap-GrhBxvVEt2tCGV9BbSq5lo

在本目录启动独立的 GPU 端到端测试（使用 GPU 容器 localhost API，不需要公开 HTTP API）：

```powershell
uv run modal run benchmark.py
```

脚本输出完整的请求级耗时和显存信息；实测过程中模型权重缓存复用 `modal-forge` Volume。


## L40S 实测与对比（2026-11-08）

本轮默认 GPU 改为 `L40S`，使用 vLLM-Omni 0.32.0 / MOSS-Image-SFT / 1024×1024 / 50 步，与 RTX PRO 6000 原测试条件一致。

| 指标 | RTX PRO 6000（历史实测） | L40S（本轮实测） |
| --- | ---: | ---: |
| 服务启动至就绪 | 40.03 s | 55.05 s |
| 单张请求 | 14.88 s | 25.54 s |
| 同时发两请求，各自完成 | 14.82 / 29.67 s | 26.24 / 52.72 s |
| 双请求全部完成 | 约 29.67 s | 52.72 s |
| GPU 使用显存（结束时，不是峰值） | 未记录 | 32575 MiB / 46068 MiB |

- 本轮 Modal 运行：https://modal.com/apps/yuqueaaa/main/ap-qLagIuymfQtKmyW6AMiPnj
- 本轮结构化记录：`results/vllm_l40s_20261108T074135Z.json`
- 单张耗时 RTX PRO 6000 约为 L40S 的 1.72 倍速度；两请求均排队，不能据此推论批处理。
- 本轮 benchmark.py 新增自动保存 JSON；运行时自动生成不覆盖以前的结果。
- 历史 RTX PRO 6000 测试参考：https://modal.com/apps/yuqueaaa/main/ap-EDds39wS99PEai8GvLWMA6

## 固定工作流 + 提示词列表：方案草案（尚未实现）

参考兄弟项目 `modal-comfyui-caru-ini/batch_run.py` 的固定工作流、替换输入、任务状态与断点续跑，但不复制 ComfyUI 的节点图机制。

建议尽量保持最少模块：

1. `prompts.json`：每个条目只需 `id`、`prompt`，例如 10 条提示词生成 10 张图；通过固定 `seed` 规则复现。
2. `batch.py`：固定模型、尺寸、50 步、引导参数等；验证提示词、逐个调用 `/v1/images/generations`、保存图片和状态清单；重跑默认跳过已经完成的图片。
3. `results/<task_id>/`：每张图对应 PNG 和元数据，汇总成功数、失败数、总耗时，防止错误响应被当作成功。

默认先使用单 GPU 容器保持模型只加载一次，然后依次生成 10 张图。未来若需要大量图片，再做多 GPU 容器分片（一个容器一部分提示词），分别衡量吞吐量与成本。避免现在将请求级 HTTP 并发误认为 GPU 内核并行。

图片持久化的首选是 Modal Volume；如需给其他 Cloudflare 任务直接消费，可再接 R2 输出。这一批量流程目前只在讨论阶段，不应误认为已完成批量运行测试。

## 共享模块与 CPU 缓存

公共设施位于根目录 shared/；本项目的缓存校验由 model_cache.py 实现。下载使用 4 vCPU，复用原有 007-moss-image 缓存路径。新版本在启动 GPU 前校验 model-ready.json，没有完成 CPU 预备任务时会直接拒绝推理。
