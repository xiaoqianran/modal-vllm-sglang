# MOSS Image

本模型使用公共模板，模型能力为 moss-t2i。使用 vLLM-Omni，默认 1024×1024、50 步。

- model.json：模型、镜像、启动命令、权重组件、资源、参数与能力配置。
- app.py / download_model.py / run.py：公共实现的薄入口。
- 公共实现：shared/model_service.py、shared/model_cache.py、shared/model_spec.py。
- 日常任务在根目录 tasks/，素材在 inputs/，结果在 outputs/<project>/<scene>/。

从仓库根目录执行：

```powershell
uv run python 001-moss-image/run.py --task tasks/003-stop-rain-001-rain-bus-stop.json --dry-run
uv run python -m modal run 001-moss-image/download_model.py
uv run python -m modal deploy 001-moss-image/app.py
uv run python 001-moss-image/run.py --task tasks/003-stop-rain-001-rain-bus-stop.json
```

下载器 --check-only 检查 CPU 缓存。模型保留部署名称 Moss Class，SDK 与私有 HTTP 共用引擎。
完整任务格式、恢复机制与验证说明见 [根目录说明](../README.md)。
本轮代码迁移没有触发云端部署或付费 GPU 验收。
