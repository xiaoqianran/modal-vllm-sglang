# Pika H3 Flex

本模型使用公共模板，模型能力为 pika-t2va / pika-fl2va。使用 SGLang Overlay，CPU 先物化基础组件。固定 6 步、5–15 秒；首尾帧任务参考 tasks/004-pika-frames-demo.json。

- model.json：模型、镜像、启动命令、权重组件、资源、参数与能力配置。
- app.py / download_model.py / run.py：公共实现的薄入口。
- 公共实现：shared/model_service.py、shared/model_cache.py、shared/model_spec.py。
- 日常任务在根目录 tasks/，素材在 inputs/，结果在 outputs/<project>/<scene>/。

从仓库根目录执行：

```powershell
uv run python 002-pika-h3-flex/run.py --task tasks/004-pika-video-demo.json --dry-run
uv run python -m modal run 002-pika-h3-flex/download_model.py
uv run python -m modal deploy 002-pika-h3-flex/app.py
uv run python 002-pika-h3-flex/run.py --task tasks/004-pika-video-demo.json
```

下载器 --check-only 检查 CPU 缓存。模型统一注册为 Model Class，SDK 与私有 HTTP 共用引擎。
完整任务格式、恢复机制与验证说明见 [根目录说明](../README.md)。
本轮代码迁移没有触发云端部署或付费 GPU 验收。
