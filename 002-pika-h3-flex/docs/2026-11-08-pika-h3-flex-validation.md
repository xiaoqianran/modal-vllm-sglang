# Pika H3 Flex｜Modal Run 真实验证与故障记录（2026-11-08）

> 历史实验记录：下文所有 `run_batch.py`、临时 `modal run` 命令仅说明当时的实验过程，旧入口已经删除。新的唯一 GPU 入口是先 `python -m modal deploy app.py`，再使用 `python run.py` 调用部署的 `Pika` Class；下文不再作为当前操作教程。

## 一、当前结论

截至本次检查，`002-pika-h3-flex/run_batch.py` 的 Modal Run 入口已修复并通过 SGLang Diffusion CLI 预检查；**尚未成功生成 MP4**。

**实际阻塞：RTX PRO 6000（96GB）在 `--performance-mode speed`、不量化、不 CPU Offload 的条件下，加载 FL2VA Transformer 时 CUDA OOM。** 这不是图片/提示词错误，也不是最早那次的 CLI 参数解析错误。

用户当前要求：**先记录到文档，不再使用更大 GPU 继续测试。** 已停止随后启动的 B200 测试会话；不要把该中断的会话描述为成功生成。

## 二、测试对象

| 项目 | 实际配置 |
| --- | --- |
| 输入目录（PC2） | `D:\a_programming_list\Ai-application\A-modal\modal-vllm-sglang\001-moss-image\results\storyboard\episode09\20261108T094144Z` |
| 图片 | `09-01.png`，3,147,861 字节 |
| Prompt 来源 | `001-moss-image/storyboard/episode09/prompts/09-01.txt`，并改写为镜头运动提示词 |
| 运行任务 | `002-pika-h3-flex/tasks/episode09_single_test.json`（本地测试文件，不作为通用样例提交） |
| 运行入口 | `002-pika-h3-flex/run_batch.py` |
| 模型 | `Pika-Labs/Pika-H3-Flex` |
| 模式 | FL2VA，首帧与尾帧都使用 `09-01.png` |
| 推理配置 | 480p、5 秒、6 步、`performance-mode=speed` |
| 原先 GPU | `RTX-PRO-6000`，约 95.01 GiB 可见显存 |
| 目标 | 仅验证 1 条镜头，不继续批量 |

## 三、真实执行与定位过程

### 1. 第一阶段：旧 SGLang 启动入口报错

最初使用：

```bash
sglang serve --model-path Pika-Labs/Pika-H3-Flex --model-variant fl2va --performance-mode speed
```

镜像是 `lmsysorg/sglang:v0.5.23`，实际 CLI 输出：

```text
sglang serve: error: unrecognized arguments: --model-variant fl2va --performance-mode speed
RuntimeError: SGLang exited while loading: 2
```

**原因：** 这里调用的是不支持上述 Diffusion 参数的 `sglang serve` 入口。退出发生在参数解析阶段，**不是** OOM。

首次运行记录：https://modal.com/apps/yuqueaaa/main/ap-8xx1cAEjrZMcSmtxpLOhdh

### 2. 第二阶段：已修复 CLI 与模型注册

改用 `lmsysorg/sglang:dev`，显式调用 Diffusion CLI：

```bash
python -m sglang.multimodal_gen.runtime.entrypoints.cli.main serve \
  --model-path Pika-Labs/Pika-H3-Flex \
  --performance-mode speed \
  --host 127.0.0.1 --port 30010
```

不再额外传入 `--model-variant fl2va`；当前模型默认加载 FL2VA 分区，可支持 T2VA / FL2VA。

使用 **CPU 容器**执行：

```powershell
uv run modal run run_batch.py --preflight
```

预检查返回：

- `cli_exit_code: 0`
- `supports_performance_mode: true`
- 找到 Pika 专用单元测试、`registry.py`、`model_overlay.py`、`minimax_h3_pipeline.py` 等相关实现。
- 说明运行环境可以解析正确的 Diffusion 命令并识别模型注册信息；**不表示 GPU 一定能运行成功**。

预检查运行记录：https://modal.com/apps/yuqueaaa/main/ap-Du5XMhReWLIyMNk6k1mY4Z

相关代码提交：[e17ceb4](https://github.com/xiaoqianran/modal-forge/commit/e17ceb4)。

### 3. 第三阶段：模型下载成功，加载时报 CUDA OOM

使用原来的 96GB GPU，执行单镜头测试后，日志已经进入模型专用 pipeline：

```text
Using pipeline from model_index.json: PikaH3FlexPipeline
Model path: .../Pika-Labs__Pika-H3-Flex-.../FL2VA
Loading required components: ['processor', 'text_encoder', 'tokenizer', 'video_vae', 'audio_vae', 'transformer']
Loaded text_encoder: MiniMaxH3Qwen3VLEncoder
model size: 48.09 GB, vram: 48.09 GB
avail GPU mem: 45.67 GB
Loading transformer from .../FL2VA/transformer
```

随后：

```text
torch.OutOfMemoryError: CUDA out of memory.
Tried to allocate 294.00 MiB.
GPU 0 has a total capacity of 95.01 GiB,
of which 294.31 MiB is free.
Process 1 has 94.71 GiB memory in use.
90.97 GiB allocated by PyTorch,
2.55 GiB reserved but unallocated.
```

**直接原因：** 文字编码器占用约 48GB 显存，随后加载视频 Transformer 时显存接近用尽；VAE 等其余组件尚未完成全部加载。图片条件和视频生成请求都还未进入实际推理。

**不要据此直接宣称模型最少需要多少显存**，只能确认此镜像、此加载路径和此配置下 96GB 不足。

本次运行记录：https://modal.com/apps/yuqueaaa/main/ap-YNGlGduiOsYwi9u3ZEJ6x4

### 4. 后续 B200 会话

曾启动一次 `PIKA_GPU=B200` 的单条验证，但用户随即要求停止并先写文档。已通过 AgentDock 停止该会话。**没有完成该次测试，没有成功 MP4，也没有进一步开展更大 GPU 推理。**

## 四、缓存与费用

- 云端挂载 `modal-forge` Volume，`HF_HOME=/mnt/modal/009-pika-h3-flex`。
- 首次正确进入加载流程时，下载了模型基础组件，并在 Volume 的 `009-pika-h3-flex/hub` 目录下看到 MiniMax-H3 和 Pika-H3-Flex 的模型缓存条目。
- **已下载的权重可以随 Volume 保留并复用**，但是否缓存完整、以及 Overlay Materializer 的文件是否完整持久化，要在后续运行中验证。
- 日志中 Materializer 的目录在 `/root/.cache/sgl_diffusion/materialized_models/`，不是上述 Volume 的挂载路径；这部分是否重复构建需要进一步检查。不要认为所有物化组件已被持久化。
- `modal run` 不需要 `modal deploy`，但每次新 GPU 容器仍需重新加载模型到显存。
- **中断中的模型下载及 GPU 加载会产生实际 Modal 费用**，但此文档不估算未经核实的金额。

## 五、当前不执行的事项

遵守用户指示，当前**不继续尝试 B200/H200/其他更大显存 GPU**，不启用 CPU Offload，不量化、不批量生成其他镜头，也不改变原始分镜图片。

## 六、未来可选修复方案（仅记录，不自动执行）

1. **继续 96GB GPU**：分析 SGLang 的组件驻留策略、分阶段加载和显存临时峰值；如果接受部分组件转移到 CPU/主存，再考虑与原先“不要节约显存”的要求是否冲突。必须先解释对延迟、稳定性与结果的影响。
2. **更大显存 GPU**：仅在用户再次明确同意后，用单条 `09-01` 进行验证；其优点是有希望保留完整 BF16 与 speed 驻留策略，但并不保证成功。
3. **缓存优化**：验证 Hub 下载缓存与 `sgl_diffusion` Overlay 物化目录是否需要一并挂载到 Volume，避免每轮重复昂贵下载或物化。
4. **固定兼容环境**：当前 `lmsysorg/sglang:dev` 标签可变。未来应锁定实际验证过的镜像 digest 或提交版本，并对照 SGLang 官方 Cookbook。

官方参考：https://github.com/sgl-project/sglang/blob/main/docs/cookbook/diffusion/MiniMax/MiniMax-H3.mdx

## 七、验证状态汇总

| 检查项目 | 状态 |
| --- | --- |
| PC2 图片与 Prompt 读取 | 已验证 |
| 单条任务结构与首尾帧上传 | 已验证 |
| Modal Run 调度 | 已验证 |
| SGLang Diffusion CLI 识别 | 已验证 |
| PikaH3FlexPipeline 加载入口 | 已验证 |
| 模型下载与 Overlay 物化 | 执行过并进入组件加载；缓存完整性待验证 |
| 96GB 无 Offload 加载全部组件 | **失败：CUDA OOM** |
| 完成首尾帧视频生成 | **未成功** |
| 输出 MP4 | **无** |
| B200 实际生成验证 | **已中止，未完成** |
