# MOSS 历史 GPU 实测记录（2026-11-08）

> 以下内容原样保留 2026-11-08 的测试数据和当时的方案讨论；独立 GPU Benchmark/Modal Run 代码已移除。实际运行以当前 [README](../README.md) 为准。

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

以上为历史独立 Benchmark 的实测数据。临时 GPU Benchmark 脚本已移除；新的冷启动、显存和吞吐测试应通过已部署的服务进行，避免绕过部署。


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
