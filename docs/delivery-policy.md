# Output Delivery（交付策略）

唯一的云端交付协议是当前持久化交付协议，参见 [delivery-v3.md](delivery-v3.md)。

- 新建 HF / both 任务自动选择当前协议；显式旧版本会被拒绝。
- 原始媒体默认不重新编码，默认强制 SHA-256；可显式使用 adaptive_lossless。
- GPU 推理先提交原始媒体及元数据，完成事件仅传递交付 ID。
- 持久化 Outbox 与回执保存在独立的 delivery-store Volume V2，媒体保存在 model-store Volume V2。
- 同一个按需 CPU 媒体池负责原件、缩略图、视频封面及手动补建；原件失败与衍生资产失败独立恢复。
- 每 15 分钟运行一个统一恢复扫描，无常驻 CPU Worker。队列提示不是事实来源。
- 原始媒体的 SHA-256 验证和远程回读均使用 1 MiB 分块；同一批次尽量复用经过验证的本地快照。
- 本地交付仍是独立目的地，并非旧版云协议。历史作品与回执保留，不重新推理或删除。

部署：

```powershell
uv run modal deploy pipeline/deploy_delivery_v3.py
```

配置 HF Bucket 为 Public，Modal Secret huggingface-secret 中设置 HF_TOKEN；不要将凭据写进任务或 Git。
本地配置 OUTPUT_DELIVERY_PROVIDER=huggingface 时默认将媒体归档到 HF；显式 local 则仅保存本地。新任务注册前校验当前 App、协议、构建版本和 Volume 身份。已有冻结场景不能静默切换交付策略。
