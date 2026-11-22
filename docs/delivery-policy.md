# Output Delivery（交付策略）

实际代码入口：

1. `pipeline/s04_collect_outputs.py`：主流程仅调度、回收、发布报告
2. `orchestration/delivery.py`：每个目的地的 Future、epoch 恢复、持久状态
3. `runtime/delivery/config.py`：校验并冻结 Task 的 delivery 配置
4. `runtime/delivery/local.py`：原有的 Modal Volume → outputs/，保留 SHA256、锁和原子写入
5. `runtime/delivery/huggingface.py`：从 Volume 临时读取并 SHA256 校验，再复用 `runtime/archive/` 完成 WebP 无损压缩和 HF 上传

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

**传输边界：** 当前归档适配器由本地 Worker 的 CPU 执行，使用一次性临时文件接收 Modal Volume 原文件，然后向 HF 上传（在 `both` 时可能另外进行一次本地输出下载）。不使用 GPU，也不保留临时缓存；但 **HF-only 并非 Modal 云端直接转存，PC2 仍承担临时网络传输**。如要完全避免 PC2 中转，未来须将这层适配器换成 Modal CPU 归档任务；交付策略、状态和主流程不需要更改。

**验证范围：** 当前只有离线模拟、已有全量测试、监控 HTML 测试。实际 HF Bucket 权限、联网 SDK 上传和公开 URL 可用性尚需用户配置后单独验证。不自动部署、启动 GPU 或执行上传。

手动归档仍可使用 `uv run python -m tools.archive_to_hf ...`，与自动交付策略相互独立。

\`run.py run\` 会在登记任务前检查本地 HF 登录凭据，缺失则拒绝提交以避免浪费 GPU；\`run.py plan\` 始终离线。不要将真实 Token 写入 \`.env.example\`。

登记 HF 新任务前，`uv run python run.py run` 会检查 HF 登录凭据，避免 Token 缺失后才花费 GPU 费用。
`run.py plan` 始终离线；真实 Token 只允许保存在忽略的 `.env` 或用户自身的 Hugging Face 凭据中，不得提交到 `.env.example`。
