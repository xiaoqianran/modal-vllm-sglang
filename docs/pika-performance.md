# Pika H3 Flex 性能验收与优化边界

日期：2026-11-25；RTX PRO 6000；PC2 workspace zhangchenzer。
原生引擎固定 SGLang Nightly 1173264b58；同一已验证 Volume 缓存。

## 旧版实测：不要混淆引擎、SDK 与传输

正式 App [002-pika-h3-flex](https://modal.com/apps/zhangchenzer/main/deployed/002-pika-h3-flex)，
revision 1，部署哈希 `811d65ba6c9931c00dfeea704a7a8489d38f16df6018c0f861752a3cb763e1e6`。
原始证据保存在 PC2 `outputs/025-pika-production-acceptance-040336/`。

| 指标 | T2VA 480p | FL2VA 480p | 缩容后 T2VA 480p |
| --- | ---: | ---: | ---: |
| 引擎生成 | 24.29 s | 28.47 s | 24.54 s |
| SDK 提交至结果返回 | 36.30 s | 41.02 s | 28.18 s |
| 保存跨度（含两次 commit） | 7.717 s | 5.882 s | 1.395 s |
| 本地 MP4 下载 | 2.005 s | 2.312 s | 4.319 s |

首条 HTTP POST 15 ms、内容 fetch 12 ms；首尾帧 HTTP POST 51 ms、两个云端输入读取 2.016 s。
因此不能将 SDK 总耗时减去引擎耗时后直接称为 Modal SDK 网络延迟。保存、帧读取、
平台调度、持久化、状态轮询均在不同计时边界内。

## GPU Snapshot 真实命中

| 启动 | 调用至 GPU Ready | 引擎初始化（历史值） | 捕获 ID |
| --- | ---: | ---: | --- |
| 首次创建 | 344.37 s | 262.95 s | a07fa23ddead43af8ca0f08d2d755d81 |
| 第二次创建 | 239.88 s | 194.55 s | f3dabc645ecd466f8bc016b4cb34593a |
| 第三次自然冷启动 | 87.32 s | 194.55 s | f3dabc645ecd466f8bc016b4cb34593a |

第三次启动前容器列表为空，未重新部署；新容器 ta-01M88FMZ205CNX2YXPQ65JRVQR 的平台日志为
`Restoring Function from memory snapshot.`，同一捕获 ID，后置钩子 4.43 s。
这证明恢复真实发生，但未说明平台恢复仅耗时 4.43 s；端到端是 87.32 s。
平台可能为不同 Worker 类型生成多个快照，重新部署也可能使旧快照失效。
参考 [Modal Memory Snapshots](https://modal.com/docs/guide/memory-snapshots)。

## 低风险修改

- Pika 的内置预热由 1344×768 改为 `--warmup-resolutions 864x480`。
  保留模型合法预热时长，实际预热步数由固定 SGLang 版本决定（此前为 2/6），
  不修改真实请求六步采样、输出尺寸或时长。增加 768p/16:9 真实业务档位 1344×768，
  旧任务显式 480p 不变；未声明的 768p 比例仍拒绝。
- Pika 缩容窗口 60 秒，最少容器 0，最多容器 1；不常驻 GPU。
- 首尾帧使用挂载内精确 size/SHA-256 验证的内容，挂载缺失、陈旧或校验失败时回退
  SDK 权威读取；两帧最多两路并行。不调用 Volume reload，不读取不受约束的输入文件。
- 视频本地状态轮询从 2 s 改为 0.5 s；只涉及容器内部的只读状态请求，不增加重复生成。
- `save.attributes.commit_ms` 分别记录 submitting、running、collecting、completed 持久化耗时。
  保留四次安全屏障，不因单次保存耗时偏高而破坏恢复语义。
- 云端遥测写入最多合并 250 ms 或 128 条事件；发布/flush 控制立即结束合并窗口，
  不为此延迟业务，保持独立不可变分片与 SHA-256 验证。降低 Volume 小文件写放大。

## Offload 与传输研究

旧版两次文本编码器 layerwise offload 初始化为 28.63 s 和 24.04 s，视频 VAE 为 3.24/3.25 s。
旧版日志显示 text_encoder host pinned 46.18 GB，video_vae host pinned 4.50 GB；
480p 实际采样峰值约 79.6 GiB。Pika 使用原生六步适配器，不能用通用低步数或低精度替代。
当前不盲目修改 text_encoder residency 或 `performance-mode=speed`，需在高分辨率实测峰值
和相同请求的延迟/质量对比之后，再单独验证不同 residency。
参考 [Pika H3 Flex 模型卡](https://huggingface.co/Pika-Labs/Pika-H3-Flex)、
[SGLang MiniMax H3/Pika cookbook](https://github.com/sgl-project/sglang/blob/1173264b58/docs/cookbook/diffusion/MiniMax/MiniMax-H3.mdx)。

本地小 MP4 下载主要受首块等待影响，不能从数百 KB 小文件的吞吐量推断大文件带宽。
保留现有单遍 SHA-256、Volume V2 大文件可恢复传输、CPU 下载与 GPU 推理解耦。
不通过取消输出校验、伪造完成状态或重复生成来优化传输。

## 新版正式部署验收

revision 2，部署哈希 `b4323691a4242b9fe3cc73e3ad26b5a01f1d022cc23812ede84a23ac12c86d81`；
缓存指纹未变，部署校验 4.750 s。实测证据位于 PC2 `outputs/026-pika-light-warmup-043818/`，
包含 evidence.jsonl、timing-summary.json、modal-runtime.log 与三条完整 MP4。

- 新捕获：GPU Ready 219.247 s，初始化 178.739 s，864×480×124 帧、2/6 步预热 68.23 s。
  后置钩子 3.038 s；三个真实任务同容器同 capture ID，末次健康检查 0.891 s。
  新 revision 的跨容器快照恢复尚未重新测量；旧 revision 已真实恢复验证。
- 真实输出均为 5.167 s、H.264/AAC，完整 FFmpeg 解码、尺寸及 SHA-256 均通过。
  480p 同 prompt/seed 与旧版文件 SHA-256 完全一致。

| 指标 | T2VA 480p | T2VA 768p | FL2VA 768p |
| --- | ---: | ---: | ---: |
| 实际分辨率 | 864×480 | 1344×768 | 1344×768 |
| SDK 提交至结果返回 | 28.974 s | 73.852 s | 97.944 s |
| 引擎任务等待（含轮询） | 24.667 s | 70.580 s | 93.632 s |
| HTTP POST | 12.550 ms | 6.039 ms | 57.772 ms |
| 读取输入帧 | 不适用 | 不适用 | 0.716 s |
| 保存跨度 | 1.149 s | 0.973 s | 1.231 s |
| 四次 commit 合计 | 2.480 s | 2.577 s | 2.854 s |
| 本地下载 | 2.068 s | 2.168 s | 4.246 s |
| 下载首块等待 | 2.061 s | 2.159 s | 4.237 s |

所有云遥测 manifest 均 complete=true，errors=0，dropped=0，分片校验通过。
480p SDK 相对旧版首次样本缩短 20.2%，但旧版另有 28.18 s 样本，不能宣称稳定加速 20%。
保存跨度相较旧版 7.717 s/1.395 s 已降至 1.149 s；Volume 时延存在波动，不能单独归因于合并写入。
768p 首尾帧读取较旧版 480p 2.016 s 样本更短，但两种分辨率与挂载状态不同，不作严格 A/B 结论。
小 MP4 下载仍主要等待首块；未改变产物下载协议及验证，尚无稳定下降证据。
本轮显存采样整体峰值 86067 MiB（约 84.05 GiB），保留现有 offload/精度策略；
480p 与 768p 文生 manifest 采样峰值分别为 80863/83339 MiB，采样间隔 5 s。
不宣称完整画质主观评测已完成。768p 仅验收 16:9、约 5 s，其他比例及更长时长不作结论。

回归：PC2 全量 pytest 319 passed、206 subtests passed、18 warnings。

## 快照与预热全因素对照

2026-11-25 对同一 768p 任务执行四组共 12 条真实视频，分别验证 GPU Snapshot 与 480p 预热开关。详见 [快照与预热全因素实测](pika-snapshot-benchmark.md)。
