> 历史试点方案：本文描述 0.3.0 时期的独立共享卷与旧部署路径，非当前默认配置。现行方案参见 `docs/model-download.md`：`model-store` Volume V2 与 `sub_path=/inference`，上层依赖 `modal-benchmark==0.4.0`。

# 共享模型 Volume V2：双卷迁移与回退

本仓库已经支持普通 Hugging Face 模型按服务逐个迁移到共享 V2 Volume，保留既有服务可写 Volume。模型下载、`model-ready.json` 发布、独立 CPU 检查和 GPU 服务启动使用相同的路径校验逻辑。

## 存储边界

| 角色 | 默认 Volume | 容器挂载 | 访问权限 |
|---|---|---|---|
| 通用模型权重 | `modal-model-store-v2`（V2） | `/mnt/models` | GPU/验证/发布容器只读；benchmark 下载器可写 |
| 服务数据、ready 记录、遥测、输出 | `modal-vllm-sglang`（沿用原版本） | `/mnt/modal` | 仍可写 |
| ComfyUI 业务输出 | 其原有输出 Volume | 不变 | 不变 |

ComfyUI 下载入口通过 `MODEL_VOLUME_NAME=modal-model-store-v2` 选择相同共享卷。无需引入数据库、网关或新下载服务。默认没有切换 ComfyUI 的生产卷，以免当前 Workflow 意外丢失缓存。vLLM/SGLang 按 **model.json** 单服务设置 `"model_storage": "shared"`；未设置时全部保持 `legacy` 旧模式。

共享模式会将 `model-ready.json` 留在原服务缓存根目录，但每个组件改为 `shared_path`（相对 `/mnt/models`），GPU 验证同样核对文件名和大小，并传递 `/mnt/models/...` 给 `{model_path}` / `{text_encoder_path}` 等参数。共享模式的 ready 记录拒绝旧 `path` 字段；旧模式拒绝 `shared_path`，避免静默指向错误权重。

## 两条迁移路径

**A. 已有 ComfyUI HF 原生快照：零复制接管，优先选用。**

确保共享卷存在 `models--<owner>--<repo>/snapshots/<40位commit>/`，且每个快照文件已由上游发布。将对应服务的 `model.json` 设置 `model_storage=shared`，固定每个组件的 40 位 commit，然后运行：

```powershell
uv run python tools/attach_shared_model.py models/005-minimax-h3-image/model.json
uv run python tools/attach_shared_model.py models/005-minimax-h3-image/model.json --execute
uv run python tools/check_shared_cache.py models/005-minimax-h3-image/model.json
```

第一条命令默认只输出执行计划。第二条只用 CPU、以只读形式挂载共享权重、核对源快照与必需文件，并将就绪清单写入可写服务卷；不会复制和重新下载权重。实际业务模型的共享快照尚未就绪时，这一步会明确报错，不能跳过。

**B. 模型尚未保存在共享卷：通过 benchmark 下载。**

```powershell
uv run python tools/download_models.py models/005-minimax-h3-image/model.json --workers 3
uv run python tools/download_models.py models/005-minimax-h3-image/model.json --workers 3 --execute
uv run python tools/check_shared_cache.py models/005-minimax-h3-image/model.json
```

`modal-benchmark==0.3.0` 在 V2 卷的独立下载目录中按清单发布固定版本权重；专用 CPU 发布容器只读挂载模型卷并写入旧服务卷上的 `model-ready.json`，最后另一个 CPU 容器再次完整核对。已有业务输出、遥测和 Pika 派生文件位置不变。**此路径当前按完整下载清单生成身份**：不同文件筛选组合可能重复占空间；对已有 HF 原生快照优先使用 A，不能宣称跨所有清单自动按文件去重。

以上示例里的 MiniMax 只表示实际服务配置的命令写法；**尚未把 MiniMax 的全部大权重迁移**，不要在生产模型尚未准备完毕时部署。待清单与引擎启动均验收后，才将对应服务配置正式切为共享模式并滚动部署。

## Tiny-GPT2：已实际运行的 CPU 试点

这份假服务仅用于测试存储，不启动假的 `python -m server` GPU 命令：

```powershell
# 从 ComfyUI 已发布的同一份模型权重零复制接管
$env:MODEL_RUNTIME_VOLUME_NAME = "modal-model-metadata-stable-pilot-v2-20261122"
uv run python tools/attach_shared_model.py tests/fixtures/shared-tiny-pilot.model.json --execute
uv run python tools/check_shared_cache.py tests/fixtures/shared-tiny-pilot.model.json
Remove-Item Env:MODEL_RUNTIME_VOLUME_NAME
```

默认共享卷名就是 `modal-model-store-v2`。该卷已由 ComfyUI 的试点清单写入 Tiny-GPT2 的 `config.json`、`pytorch_model.bin` 和 `tokenizer_config.json`，总体约 2.51 MB。试点仅把服务元数据写入独立的测试 Volume，不影响正式的 `modal-vllm-sglang`。

另外也实际验证过 `tools/download_models.py --execute` 在独立 V2 权重卷下载 9 个 Tiny-GPT2 文件，共 4,734,064 字节，并在全新 CPU 容器通过 `verify_cache` 和 `command_argv`。这种端到端检查**不是 GPU 推理成功证明**。

## 回退与上线约束

1. 优先挑一个尚无活跃任务的服务做首批正式迁移。现有模型文件和生产 Volume 保留，禁止直接删旧数据。
2. 每个模型必须先在共享卷中完整就绪，再在其 `model.json` 启用 `model_storage=shared` 并验证 GPU 启动/实际推理；不能把所有服务一次性切换。
3. 若共享发布替换原 `model-ready.json`，旧路径式清单会保存在同目录的 `model-ready.legacy.json`（只首次写入，不覆盖）。回退需将配置改回 `legacy`，并由人工确认后将旧清单重新发布为 `model-ready.json`，再做 CPU 核验与滚动部署。
4. SGLang Pika Overlay 的 `materialized_models` 当前仍须可写，配置校验直接拒绝普通 shared 模式；不能将整个 `/mnt/modal` 改成只读。
5. 运行中的 GPU/内存快照必须用新的配置与缓存指纹重新构建；校验出错应停止发布而非自动回退到在线 HF 下载。
6. 只有所有消费者转移并通过 GPU 冷启动、实际生成、恢复与回退测试后，才能规划旧 Volume 的清理。

## 环境变量（仅作为选择器，不迁移文件）

- `MODEL_SHARED_VOLUME_NAME`：改变共享 V2 卷名，默认 `modal-model-store-v2`；生产应与 ComfyUI 的 `MODEL_VOLUME_NAME` 一致。
- `MODEL_RUNTIME_VOLUME_NAME`：改变服务可写卷，默认 `modal-vllm-sglang`；通常仅用于独立试点，正式运行保留原名称。

注意：`orchestration.Provisioner` 仍通过现有 `model-ready.json` 远程指纹检查，并在缓存缺失时调用 `tools/download_models.py --execute`。开启 shared 后下载将进入共享 V2 卷、ready 记录仍回写原可写卷。旧的手动 `MODEL_PREPARE=1` 入口对于未就绪共享模型不会自行下载，请使用上面两个明确入口。
