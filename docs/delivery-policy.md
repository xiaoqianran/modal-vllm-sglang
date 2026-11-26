# Output Delivery（交付策略）

实际代码入口：

1. `pipeline/s04_collect_outputs.py`：主流程仅调度、回收、发布报告
2. `orchestration/delivery.py`：每个目的地的 Future、epoch 恢复、持久状态
3. `runtime/delivery/config.py`：校验并冻结 Task 的 delivery 配置
4. `runtime/delivery/local.py`：原有的 Modal Volume → outputs/，保留 SHA256、锁和原子写入
5. `runtime/delivery/huggingface.py`：PC2 仅向已部署的 Modal CPU 归档函数发送素材元数据，不读取 Volume 文件
6. `pipeline/deploy_assets.py`：独立的无 GPU Modal CPU App，使用只读挂载同一 Volume 与 `huggingface-secret`
7. `runtime/delivery/cloud_cpu.py`：在云端完成文件完整性校验、无损压缩、HF 上传、SHA256 验证

## 默认：Hugging Face

本地 `.env` 和仓库的 `.env.example` 都配置：

```dotenv
OUTPUT_DELIVERY_PROVIDER=huggingface
HF_ASSET_BUCKET=seachen/modal-vllm-sglang-assets
```

新 Task 不写 `delivery` 时归档 HF，不写本地 `outputs/<name>/`；已登记的历史 Task 仍然按冻结的旧计划交付。
新任务如需本地输出，可以在 Task 中设置 `"delivery": {"provider": "local"}`，或修改 `.env` 为 `OUTPUT_DELIVERY_PROVIDER=local`。
如果不存在 `.env` 且没有环境变量覆盖，保留兼容默认 `local`。

可选 `delivery`（在 Task JSON **顶层**）：

```json
{
  "delivery": {
    "provider": "both",
    "bucket": "seachen/modal-vllm-sglang-assets",
    "compression": "adaptive_lossless",
    "min_saving": 0.05,
    "verify": "sha256"
  }
}
```

`provider` 支持 `local`、`huggingface`、`both`。选择 HF/both 时，`bucket` 默认使用 `seachen/modal-vllm-sglang-assets`，也可显式覆盖。默认压缩为 `adaptive_lossless`（无损 WebP，经像素比较且至少节省 5% 才采用），视频不压缩；`verify` 默认 `sha256`，可选 `size`。

**重要安全/运行前提：** HF Bucket 必须由用户预先创建为 **Public**，且 PC2 Worker 运行环境中有 `HF_TOKEN`（具有 Bucket 写入权限）。不要把 Token 写进 Task JSON 或 Git。HF 失败不清除 Volume 原文件，用户排查后可运行：

```powershell
uv run python run.py resume outputs/<name>
uv run python run.py worker
```

`state.json` 每个 job 分别保留 `local_artifact` 和 `cloud_archive`，以及 `download_error` 和 `archive_error`。只有所有被请求的交付目标成功后才标记整个场景 `finished_at`。如果 `both` 中 HF 失败，已成功的本地文件可以继续复用；**不会重新提交 GPU 生成**。

`huggingface` 模式不将图片或视频写入 `outputs/<name>/`；监控 HTML 可以显示公开 URL 的图片或视频。任务状态/日志依然写入 `.runtime/`。已经登记的任务交付策略冻结在 plan/hash 中，不能直接改 JSON 切换交付渠道；旧任务继续本地交付。

**传输边界：** HF-only 交付采用 `Modal Volume → Modal CPU → Hugging Face Bucket`。PC2 Worker 只提交 `artifact` 元数据和策略，不下载素材、无临时视频文件、不持有 HF Token。CPU App 从 Modal Secret `huggingface-secret` 注入 `HF_TOKEN`，仅读取已存在的 Volume（`create_if_missing=False`，避免意外创建空 Volume）。本地 `local` 是历史兼容/显式选择的独立交付目标，不是 HF 上传的中转站。

先确保目标 Modal 环境内已存在**实际存储生成结果的 Volume** 和 `huggingface-secret`（含 `HF_TOKEN`），再部署无 GPU CPU App：

```powershell
uv run python -m modal volume list
uv run python -m modal secret list
uv run python -m modal deploy pipeline/deploy_assets.py
```

新的 `run.py run` 在注册 HF 新任务前会调用 Modal CPU `check_bucket`，核实 Secret 的身份、Bucket 和公开状态；不再读取 PC2 的本地 HF Token。GPU 生成与 HF 归档分离，CPU 归档失败时仅恢复交付，不重新进行 GPU 推理。

**验证状态：** Modal CPU 代码和离线集成测试已验证。正式部署需要对应 Modal 环境存在 Volume；完整云端 Volume→HF 端到端验证须在该环境完成。不会自动创建空 Volume。


手动归档仍可使用 `uv run python -m tools.archive_to_hf ...`，与自动交付策略相互独立。


登记 HF 新任务前，`uv run python run.py run` 会检查 HF 登录凭据，避免 Token 缺失后才花费 GPU 费用。
`run.py plan` 始终离线；真实 Token 只允许保存在忽略的 `.env` 或用户自身的 Hugging Face 凭据中，不得提交到 `.env.example`。


## SHA-256 streaming verification

For new HF-only deliveries, the CPU archiver hashes the committed source once
in 1 MiB blocks and compares it with the generation receipt before upload.
If the stored encoding changes, hash the encoded file separately. The archive
receipt reuses the verified stored digest. SHA-256 verification reads the
public gallery URL in 1 MiB blocks, checks both byte count and digest, and
does not write a full verification copy or transmit credentials to the URL.
This still reads the entire remote object; it is not a metadata-only check.

PC2 local I/O microbenchmark, 256 MiB synthetic source, five alternating runs:
median old 1.4966 s, new 0.5200 s, single SHA-256 953 MiB/s. The test simulates
remote readback using a local stream and excludes HF network time. The new
path retains the full SHA-256 integrity check.

2026-11-26 huaianer cloud smoke test: 12,602,631-byte synthetic PNG,
Volume -> CPU -> HF upload and full SHA-256 readback, 10.802 s RPC wall.
Independent anonymous PC2 readback 9.466 s; matching SHA-256. Both test
objects cleaned. This is a single smoke test, not a network A/B benchmark.


## Archive throughput screening (2026-11-26)

Real huaianer CPU archive calls, four independently generated 1024x1024 PNGs
(~12 MiB) per batch, no compression, full SHA-256 readback; six batches ordered
1/2/4/4/2/1, different random bytes for each batch:
- 1 concurrent: 11.527 / 13.722 s (median 12.624).
- 2 concurrent: 5.587 / 8.732 s (median 7.160).
- 4 concurrent: 4.814 / 6.096 s (median 5.455).
All 24 archived and verified; all test HF objects removed and Volume cleanup
completed without reported errors. Retain max_containers=4; eight was not tested.
Timings include RPC, compression=none, upload, and anonymous SHA readback;
they are not isolated upload throughput measurements.

PC2 read-only PNG encoder screening (lossless, exact=True, pixels compared):
old method=6 quality=80 vs candidate method=2 quality=20:
- 1024x1024: 1.53975 -> 0.21687 s, 1043136 -> 1066524 bytes.
- 2048x1152: 2.57329 -> 0.58521 s, 2738522 -> 2772334 bytes.
- 5472x3072: 16.81873 -> 3.75917 s, 18624906 -> 18808806 bytes.
Small samples, local CPU, not an isolated cloud A/B. Keep method=2 quality=20
as a balanced candidate: near-original compression ratio, lower encode effort,
no pixel loss. Skip existing WebP and animated PNG; decode and compare only
if savings justify adopting the candidate. A changed encoder may generate a
new content address for an image archived by the older encoder.

Reuse is per project and stored-byte hash: verify the anonymous public URL
by full SHA-256 before skipping upload. Only HTTP 404 permits creation;
other errors fail closed. Concurrent first publications are not single-flight.
