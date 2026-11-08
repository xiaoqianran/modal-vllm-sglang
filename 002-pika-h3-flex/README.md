# 002 Pika H3 Flex · SGLang fixed workflow

## 模型下载与推理严格分离

CPU 下载使用和推理一致的 SGLang Diffusion 镜像与模型注册机制。它会处理
Pika Overlay、官方锁定的 MiniMax-H3 基础组件，以及模型物化目录；
不再通过直接下载 Pika 仓库全部文件来代替模型准备。

第一次运行先检查环境，再执行 CPU 模型准备：

    uv run modal run download_model.py --preflight-only
    uv run modal run download_model.py

下载和物化完全成功后，会生成共享 Volume 中的
`/009-pika-h3-flex/model-ready.json`，包含逐文件大小清单。
失败时不主动删除上一份完成标记，但推理启动时会重新校验文件清单，
任何损坏或缺失均拒绝执行。

随后运行 GPU 批处理：

    uv run modal run run_batch.py --workflow workflow_t2va.json --task tasks/t2va_demo.json

也可以按需部署 HTTP 服务：

    uv run modal deploy app.py

GPU 推理必须使用 `HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`，
并共享 `SGLANG_DIFFUSION_CACHE_ROOT` 和 `HF_HOME`。SGLang 的启动模型标识
保留为 `Pika-Labs/Pika-H3-Flex`，以匹配其官方 Overlay 注册表。
缓存不存在或未通过检查时应直接失败，不能在 GPU 容器中补充下载。
已准备的模型仍需要在 GPU 启动时重新加载至显存。

注意：不要同时运行两份 CPU 下载程序，也不要在推理期间升级模型或更换
SGLang `dev` 镜像。Modal Volume 不提供分布式文件锁。
本次修改不会自动执行下载或 GPU 推理。

Pika-H3-Flex native six-step stochastic re-noising; both T2VA (text-to-video-and-audio) and FL2VA (first+last frames + prompt) share one FL2VA model partition.

## System

- app.py: Modal RTX PRO 6000 (96GB), SGLang memory mode (RTX PRO 6000 96GB): text_encoder and VAE layerwise offload, video_vae=36 resident layers; no explicit weight quantization.
- proxy.py: public port 30011, waits for SGLang health on internal port 30010, stages inline frame bytes in the SAME running container and translates them into official keyframe conditions.
- workflow_t2va.json / workflow_fl2va.json: fixed video parameters (5 seconds, 480p short edge, six steps).
- tasks/*.json: prompt lists. FL2VA items have first_frame + last_frame relative to this module directory.
- batch_run.py: run every item, resume completed and unchanged MP4s, download files, save run JSONs.
- generate.py: legacy single-request client, for low-level testing; does not upload local images.

## Deploy

    cd 002-pika-h3-flex
    uv sync
    uv run modal deploy app.py

Service: modal-forge-009-pika-h3-flex-fl2va
Default GPU: RTX-PRO-6000; min_containers=0, max_containers=1, scaledown_window=120.
Modal Proxy Auth is disabled; the endpoint is publicly accessible and does not need tokens.
Weights cache in existing modal-forge Volume, /009-pika-h3-flex subpath.

## Batch generation (Windows PowerShell)

    $url = "https://yuqueaaa--modal-forge-009-pika-h3-flex-fl2va-serve.us-east.modal.run"

    uv run python batch_run.py --endpoint $url --workflow workflow_t2va.json --task tasks/t2va_demo.json
    uv run python batch_run.py --endpoint $url --workflow workflow_fl2va.json --task tasks/fl2va_demo.json

To generate 10 clips, add 10 entries to the task JSON. All fixed video parameters remain in the workflow JSON.

FL2VA mode uses two images with frame_index=0 and -1, role=keyframe. The local client base64-encodes PNG/JPEG/WebP images into ONE video submission; the HTTP gateway saves them to per-container /tmp, translates to file:// URI and forwards the request to internal SGLang. No public image host, R2, or extra upload call is needed.

Output: results/<mode>/<task name>/<id>.mp4, <id>.json, report.json. Completed jobs are skipped ONLY when prompt, workflow, seed, model, frames and MP4 checksum still match. Changed inputs regenerate output, failures write <id>.error.json. Start with one GPU container and serial requests to avoid duplicate GPU loading.

Important: the SGLang video job registry lives in the GPU container. The HTTP service now uses a 120-second idle window to reduce accidental scale-down between polling calls. Container interruptions can still invalidate in-memory jobs; rerun the task to recover saved outputs.

Do not use MiniMax-H3 original Euler sampler or force flow_shift; Pika model requires its trained six-step schedule.

## Verification and limits

SGLang official reference:
https://github.com/sgl-project/sglang/blob/main/docs/cookbook/diffusion/MiniMax/MiniMax-H3.mdx

The batch format and local tests are not proof of actual successful GPU model inference. Full GPU model loading, VRAM peak, and MP4 validation must be separately checked.

The API is public with no authentication. Anyone who knows the URL may trigger GPU inference and incur cost. Keep the endpoint confidential or add access control when needed.

## Modal run (recommended batch entrypoint, without deployment)

Compatibility fix (2026-11-08): The original `lmsysorg/sglang:v0.5.23` language-model CLI rejected Pika-specific Diffusion arguments. Both inference entrypoints use `lmsysorg/sglang:dev` and the Diffusion CLI with `--model-path` set to the downloaded local snapshot. The default FL2VA partition handles both T2VA and FL2VA. To verify the Diffusion runtime and Pika model registry without allocating a GPU, run `uv run modal run run_batch.py --preflight`. The dev Docker tag is mutable and should be pinned to a stable compatible release when available.


run_batch.py is independent from app.py. Each invocation starts one Modal GPU container, loads Pika H3 Flex only once, sequentially generates videos, then exits. No HTTP URL or Proxy Token required. app.py stays available as an optional HTTP service.

Both entrypoints reuse the SAME "modal-forge" Volume checkpoint cache subdirectory "009-pika-h3-flex". Cached checkpoint files survive container shutdown, but GPU-loaded weights do not.

PowerShell:

    cd D:\a_programming_list\Ai-application\A-modal\modal-vllm-sglang\002-pika-h3-flex
    uv run modal run run_batch.py --workflow workflow_t2va.json --task tasks/t2va_demo.json
    uv run modal run run_batch.py --workflow workflow_fl2va.json --task tasks/fl2va_demo.json

To run ONLY the first item:

    uv run modal run run_batch.py --workflow workflow_fl2va.json --task tasks/fl2va_demo.json --limit 1

Images may use paths relative to this module or absolute paths. Images are uploaded once to the existing Volume rather than passed as Base64 Modal arguments. The GPU uses those images as first/last keyframes through file:// conditions. This path uses six Pika steps, SGLang performance=memory with text_encoder,vae layerwise offload and video_vae=36 resident layers; no explicit weight quantization. CPU↔GPU transfers can increase latency.

Results: results/<mode>/<task name>/<run id>/*.mp4, *.json, report.json.
Cloud: 009-pika-batch/<mode>/<task name>/<run id>/ in the Volume.
After every completed video the cloud output is committed. Use the SAME --run-id on subsequent invocations to resume. Matching videos and hashes are skipped; changed prompts, frames or workflow regenerate. Failures stop the batch without starting further items.

Offline tests (no GPU):

    uv run python -m unittest discover -s tests -v

## 共享模块

shared/ 包含 Modal 镜像、Volume、进程监控、文件清单和结果存储公共能力。Pika Overlay、六步采样和关键帧代理仍是 Pika 专属逻辑。为复用权重，云端 009-pika-h3-flex 缓存路径保持不变。
