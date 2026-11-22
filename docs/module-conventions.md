# 模型模块与命名约定

## 模型边界

每个模型必须保留自己的 `model.json`、`app.py` 和 `download_model.py`。
模型特有的权重格式、Adapter/Overlay、参数约束与镜像配方一律放在该模型的
`model_impl/`。多个模型真正通用的代码才进入 `runtime/`。

公共下载器不应通过 `sys.path.insert` 固定某个模型目录。需要专属 Overlay 时，
`model.json` 声明 `cache_implementation`，例如
`models.002-pika-h3-flex.model_impl`。该包提供以下通用接口：

| 接口 | 职责 |
|---|---|
| `download_sources(spec)` | 返回固定版本与白名单的模型下载源 |
| `build_publisher_image(spec, modal_version=...)` | 创建独立 CPU 发布/物化镜像，不启动 GPU |
| `publish_cache(spec, manifest, base, root, volume)` | 只在完整校验之后发布 ready 清单 |

普通 Hugging Face 模型无需自定义 `cache_implementation`，由通用
`runtime/config/cache_publication.py` 处理。
`validator_module` 单独声明模型的 `model_impl.params`，不依赖缓存实现。
已归档的模型通过 `download_policy: archived` 要求额外确认，公共下载器不按
具体模型名称判断安全策略。

## 公共模块

| 模块 | 单一职责 |
|---|---|
| `runtime/config/model_spec.py` | 验证模型配置与生成参数 |
| `runtime/config/model_cache.py` | 模型缓存完整性与历史清单迁移 |
| `runtime/config/cache_inventory.py` | 文件清单与大小校验 |
| `runtime/config/cache_publication.py` | Hugging Face 缓存清单发布与共享 Volume 引用 |
| `runtime/serving/model_runtime.py` | GPU 模型运行生命周期 |
| `runtime/serving/legacy_cache_preparer.py` | 旧版 CPU 准备入口，仅为迁移兼容保留 |
| `runtime/serving/process_lifecycle.py` | 进程启动、健康检测与退出 |
| `runtime/serving/inference_api.py` | 与推理引擎无关的图像/视频请求协议 |
| `runtime/tasks/task_identity.py` | 标识校验与稳定内容哈希 |
| `runtime/tasks/result_integrity.py` | 生成结果完整性校验 |
| `runtime/tasks/result_summary.py` | 完成、跳过、失败与未执行数量统计 |
| `orchestration/model_capabilities.py` | 模型能力发现与参数适配 |

旧 `runtime/tasks/batch.py` 只保留兼容导出；新代码统一引用上表的模块。
`VLLMImageClient` 是图像协议客户端历史别名，规范名称是
`ImageGenerationClient`；`runtime/config/resolution.py` 的
`apply_render_preset`、`runtime/utils/outputs.py` 的
`validate_path_component` 是规范函数名。

## 发布和兼容边界

- 统一下载入口：`tools/download_models.py`，各模型目录中的
  `download_model.py` 为薄入口
- `tools/download_azure.py` 仅保留旧命令兼容转发，**不表示固定 Azure**
- **不改动** Modal App/Volume 名称、现有缓存目录、`model-ready.json`
  结构、已发布任务 JSON 字段或 GPU 镜像固定 digest
- 名称整理通过离线单元测试，不等价于 Modal 云端构建、Overlay 物化和 GPU
  真实生成的端到端验收
