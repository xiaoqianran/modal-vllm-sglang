# Pika GPU Snapshot 瘦身与短预热实测（2026-11-25）

## 决策

保留正式 A 配置：GPU Snapshot + 480p/5s 预热。G（4s 预热）作为候选，不直接替换；不引入 E 的固定等待或 F/I 的 Sleep/Wake；H 的 CPU Snapshot 不是 GPU Snapshot 的等价替代。结合[前一轮四因素实验](pika-snapshot-benchmark.md)，稳定批量优先热容器复用；频繁发布且预期很少复用快照的场景可另评估 B（off + 预热），不要为快照命中主动缩容。

## 实验设计

- 基线 `1ac740cd3ec588e1e1345689572af25a8c1b942b`，SGLang Nightly `1173264b58`，PC2 Modal profile `zhangchenzer`，六个独立 App。RTX PRO 6000 ×1、4 CPU、48 GiB 请求内存、min=0/max=1、60s 缩容；同一模型缓存指纹 `b9bd0bc45e43db106020dcd269de7eafbc2e12d6b7f215890aa14d9a5b76f85b`。
- 同一 `tasks/004-pika-video-demo.json` 首条 prompt、seed 1101，768p/16:9/5s，layerwise offload、原生六步采样、`TORCHINDUCTOR_COMPILE_THREADS=1`。SDK 直接提交业务，不以健康探针激活 GPU。首轮含冷/热各一条；轮询至旧容器自然消失后进行二次冷启动；未命中补第三次，最多三轮；自然退出观察窗口 240s。
- A/E/F 预热实际 864×480×124f，G/I 设置 `--warmup-num-frames 96`，合法目标4s、实际对齐至107f，均为2/6步。真实业务 1344×768、6步不变。H 仅在 CPU pre-hook 保存 spec/capture ID，GPU 子进程由后置 hook 正常启动；不宣称 CPU 快照保存了 GPU 权重。
- 固定版本 Diffusion 原生 `POST /release_memory_occupation` 和 `POST /resume_memory_occupation`。MemoryOccupationController 跳过 layerwise 管理模块，在非 FSDP 场景清理图池、缓存并迁移可卸载模块；F/I 实际检查接口成功状态。E 增加2s等待，测试原生 idle cache release 的边际收益；未照搬 LLM 专用参数。
- 六组并发存在 Worker、存储、检查点竞争，非同一物理主机的严格配对。正式 Pika 不参与实验，不修改生产配置。

## 计时口径

SDK 延迟为直接业务提交至结果返回（不含本地下载和空闲等待）。捕获边界为 `snapshot_capture_ready` 至 `lab_post_snapshot_enter` 的秒级日志差，包含平台检查点与恢复相关开销，不等于磁盘写入时长或快照文件大小。恢复边界为平台 Restoring 日志至 `model_ready`。GPU 占用来自 nvidia-smi；cgroup memory.current 未读到，不报告直接 CPU 快照内存变化。真实命中须有不同容器 ID、相同 capture ID 及平台恢复日志，首次新捕获不计入后续命中率。

## Sleep 与缩容

F：12728→6204 MiB，release 0.684s，wake 0.033s；I：12645→6203 MiB，release 0.807s，wake 0.029s。GPU 占用下降约51%，捕获边界反而为205/206s，显著慢于首轮 A 42s/G 37s。Wake 后已验证业务正常，但不能由显存占用推断 CPU+GPU 快照大小。E 首次等待前后均12727 MiB，无额外释放证据。

F/I 在热态后均超出240s自然缩容观察窗口，应用日志已记录 Application shutdown complete；随后清理阶段自然退出。未确定根因，未进行跨容器恢复验证，不得将同容器恢复钩子执行成功冒充新容器快照命中。

## 结果

### SDK 端到端（秒）

| 方案 | 首次 | 热态 | 第二次冷启动 | 第三次冷启动 | 后续命中/尝试 | 视频数 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A GPU+5s | 299.15 | 77.32 | 326.53 | 114.69 | 1/2 | 4 |
| E A+等待2s | 299.31 | 73.28 | 362.90 | 330.57 | 0/2 | 4 |
| F A+Sleep | 479.20 | 74.02 | — | — | 0/0 | 2 |
| G GPU+4s | 338.49 | 74.36 | 285.45 | 111.48 | 1/2 | 4 |
| I G+Sleep | 480.53 | 74.42 | — | — | 0/0 | 2 |
| H CPU+5s | 297.18 | 74.39 | 236.47 | 244.22 | 1/2 | 4 |

0/0 表示缩容超时后没有后续恢复尝试，而非零命中率。

### 捕获与恢复（秒）

| 方案 | 捕获边界 | 实际预热 | 捕获前→后 GPU MiB | 恢复→Ready |
| --- | --- | --- | --- | --- |
| A GPU+5s | 42, 40 | 80.09, 81.45 | 12727→12727, 12727→12727 | 37 |
| E A+等待2s | 55, 132, 50 | 67.64, 66.73, 89.55 | 12727→12727, 12727→12727, 12726→12726 | — |
| F A+Sleep | 205 | 65.64 | 12728→6204 | — |
| G GPU+4s | 37, 32 | 68.71, 66.22 | 12646→12646, 12644→12644 | 35 |
| I G+Sleep | 206 | 65.45 | 12645→6203 | — |
| H CPU+5s | 0, 0 | 72.94, 57.38, 53.03 | — | 166 |

### 冷启动身份

| 组 | 轮次 | 容器 ID | capture ID | 命中 |
| --- | ---: | --- | --- | --- |
| a-baseline | 1 | ta-01M88JW40A94AZKB8JSSNE0J3R | ec05c131db7743fe882779504b64906f | 否 |
| a-baseline | 2 | ta-01M88KC5NJ89YHTAT7EPNSS59R | e4185b63bf16492398188405efb7b76d | 否 |
| a-baseline | 3 | ta-01M88KWW3634Z5VZW04RXWD3GR | e4185b63bf16492398188405efb7b76d | 是 |
| e-settle | 1 | ta-01M88JW7S4GXAXXQK7QN90ZRRR | 879c389773f24ec3b871b120a3255546 | 否 |
| e-settle | 2 | ta-01M88KC2S7SSX1NHQZZS5NGPJR | 2dc2d9aa68824b6499890f1d488b79cf | 否 |
| e-settle | 3 | ta-01M88KXNJPX2FF8DW7P6MYHDKR | 8b8d133f539a4b4ba721cb6adaab8665 | 否 |
| f-sleep | 1 | ta-01M88JW63EBH15E1YS26JW3THR | c2db73a411484959927be06547c2b79a | 否 |
| g-short | 1 | ta-01M88JW7M32Z31JH2R3WZF9TKR | 0ff8804c63fc47619abafbde06fd3f29 | 否 |
| g-short | 2 | ta-01M88KD4GTDNAFAD31KKM5308R | 8a5da17aa8b147c793aa20c1e32cd390 | 否 |
| g-short | 3 | ta-01M88KVG5XTEYD3MZR461W0G1R | 8a5da17aa8b147c793aa20c1e32cd390 | 是 |
| i-short-sleep | 1 | ta-01M88JW5YNK7B9Z1A5ZAKTS9BR | bbd801b4f2eb45ed92e69e3cddd226bf | 否 |
| h-cpu | 1 | ta-01M88JWB9NGWENXW6C969X0BHR | 6dcfd7bc07df46528e5c095eeb18d74d | 否 |
| h-cpu | 2 | ta-01M88KC5NJJWRQA0YRN7JZPWCR | 14526274a5464a2382bb3a26dfa94ca5 | 否 |
| h-cpu | 3 | ta-01M88KQ5J64BZD77X0XY8529DR | 14526274a5464a2382bb3a26dfa94ca5 | 是 |

### 自然缩容观察

- a-baseline: 147.36, 220.00, 126.77s（含轮询误差，非配置值）。
- e-settle: 145.56, 211.87, 113.72s（含轮询误差，非配置值）。
- f-sleep: 首次240s超时；从最后热态 info 返回到清理轮询发现消失共 399.40s（含日志收集、轮询）。
- g-short: 138.96, 183.56, 129.31s（含轮询误差，非配置值）。
- i-short-sleep: 首次240s超时；从最后热态 info 返回到清理轮询发现消失共 400.62s（含日志收集、轮询）。
- h-cpu: 145.33, 122.67, 125.23s（含轮询误差，非配置值）。

### 成片验证：20/20

全部真实业务均为1344×768、5.167s、H.264/AAC，下载SHA-256、FFmpeg解码通过，完整文件、解码视频与解码音频分别完全一致。
- file: a1ad40a8dd03fceadc9c3f5c42da9ed210487a7abd37a71fb9fc923c2739c66a
- video_decoded: c42259d589312d670f52907b95490afccb3f843f5b088d858046b326bc7634a7
- audio_decoded: fd8e3b5d216bcdf0fe146907d96d25e7442cb37a91946d9bba68c0940614a3ea

### 资源成本估算

一分钟桶 GPU 分配量积分；CPU/内存按每桶 max(请求量,平均用量) 近似，workspace billing rates。非最终实扣账单，不含未单列存储、网络及控制面开销。F/I 仅生成两条且缩容失败，不与四条工作量直接比较；容器仍可见不意味着整个退出观察期间均计费，GPU 分配时长以平台指标为准。

| 方案 | GPU 分配秒 | GPU USD | 综合资源估算 USD | 成片数 |
| --- | ---: | ---: | ---: | ---: |
| A GPU+5s | 1220.06 | 1.027 | 1.439 | 4 |
| E A+等待2s | 1427.98 | 1.202 | 1.674 | 4 |
| F A+Sleep | 670.33 | 0.564 | 0.792 | 2 |
| G GPU+4s | 1165.72 | 0.981 | 1.363 | 4 |
| I G+Sleep | 670.99 | 0.565 | 0.794 | 2 |
| H CPU+5s | 1219.52 | 1.026 | 1.399 | 4 |

总估算 USD 7.46；统计区间 2026-11-25T05:28:00+00:00 至 2026-11-25T05:56:00+00:00。

### 清理与归档

结束 2026-11-25T05:55:32.814850+00:00；6个临时 App 均 stopped；F/I 自然缩容超时，runner 总成功断言不通过（如实保留失败组）。生产 App 保持原版本。
PC2 原始证据 outputs/028-pika-snapshot-lab-1125-052846/，含20个实际验证成片、evidence.jsonl、配置、日志、调用/资源指标、comparison.json、snapshot-boundaries.json、decoded-hashes.json、实验脚本与runtime-lab.patch。

## 生产与后续

保持 A；G 可进一步采用随机化/分时多次采样验证生命周期收益。E 无可观测释放收益，不引入额外等待；F/I 需先定位退出根因再完成恢复与重复对照；H 即便 CPU 快照命中仍需要完整引擎初始化。本实验仅覆盖同一 prompt/seed 的 T2VA，不外推至所有内容、长视频或 FL2VA。

参考：[Modal Memory snapshots](https://modal.com/docs/guide/memory-snapshots)、[Modal SGLang snapshot example](https://modal.com/docs/examples/sglang_snapshot)、[SGLang 固定版本](https://github.com/sgl-project/sglang/tree/1173264b58/python/sglang/multimodal_gen)。
