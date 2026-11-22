# Modal Volume V2 跨项目模型共享试点（2026-11-22）

## 范围与边界

本试点只读检查由 `modal-comfyui-caru-ini` 下载、验证并发布的权重。
下载能力来自 `modal-benchmark==0.2.2`，没有修改 benchmark 包内部实现。
试点没有启动 GPU，也没有修改本仓库现有的 `modal-vllm-sglang` 模型 Volume、
`model-ready.json`、SGLang Overlay 或实际推理服务路径。

- 独立 V2 Volume：`modal-models-pilot-v2-20261122`
- HF 仓库：`sshleifer/tiny-gpt2`
- 固定 revision：`5f91d94bd9cd7190a9f3216ff93cd1dd95f2c7be`
- 模型文件：`config.json`（662 字节）、`pytorch_model.bin`（2,514,146 字节）
- ComfyUI 下载侧在同卷移动到 HF 原生 `blobs/snapshots`，并建立软链接
- 第二次同一 Workflow 执行结果：`CACHE HIT` × 2、`CACHE READY`
- 随后把 `tokenizer_config.json`（26 字节）加入清单，前两个文件继续
  `CACHE HIT`、只补下载新增文件；下载、凭证校验、同卷发布与任务目录清理均成功
- 独立 Volume 的 `.benchmark-downloads` 最终为空，`blobs` 有且只有这三个
  已发布文件（662、2,514,146、26 字节），`snapshots` 中为对应三个软链接
- 将试点清单移至 ComfyUI 的 `testing/pilot_shared_tiny_gpt2.models.json`，
  使用 `MODEL_CONFIG_PATH` 指定它，再次 `--check-only` 返回
  `CACHE READY: pilot-shared-tiny-gpt2 (3 models)`
- 本仓库另一个 Modal App 通过 `with_mount_options(read_only=True)` 只读挂载，并校验：
  `pytorch_model.bin` 的 SHA-256 为
  `b706b24034032bdfe765ded5ab6403d201d295a995b790cb24c74becca5c04e6`

## 只读复查命令

在本仓库根目录执行：

```powershell
uv run python scripts/probe_shared_model.py `
  --volume modal-models-pilot-v2-20261122 `
  --path models--sshleifer--tiny-gpt2/snapshots/5f91d94bd9cd7190a9f3216ff93cd1dd95f2c7be/pytorch_model.bin
```

此脚本仅使用 1 CPU 的 Modal 容器，不会下载模型、写入 Volume 或启动推理引擎。
小于等于 16 MiB 的文件会计算 SHA-256；大权重仅检查非空、路径及大小，
避免试点工具每次完整读取大模型。

## 后续迁移前提

此验证说明同一 Environment 内两个项目可以共享一份已发布权重，
**不意味着已将生产 vLLM/SGLang 推理切换到新模型卷**。

正式迁移需要独立完成：
1. 定义共享权重路径和服务级 `model-ready.json` 如何对应，保持精确 revision 与完整性验证；
2. 将权重只读 Volume 与服务输出/编译缓存可写 Volume 分开挂载；
3. Pika Overlay 保留必要的物化输出，不把它当成原始 HF 快照；
4. CPU 验证成功后再做实际 GPU 引擎启动、离线检查、推理和回退测试；
5. 证明旧 Volume 没有活跃消费者后，才考虑清理旧缓存。

Modal 的 `with_mount_options(read_only=True)` 与 commit/reload 契约见官方文档：
https://modal.com/docs/sdk/py/latest/Volume
