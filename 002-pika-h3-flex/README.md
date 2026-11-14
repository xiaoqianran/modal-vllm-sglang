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

下载器 --check-only 检查 CPU 缓存。模型保留部署名称 Pika Class，SDK 与私有 HTTP 共用引擎。
完整任务格式、恢复机制与验证说明见 [根目录说明](../README.md)。
本轮代码迁移没有触发云端部署或付费 GPU 验收。

## 异步任务和八路视频下载

现在可复用公共异步任务客户端；提交任务会启动付费 GPU 推理，
仅执行 `--collect`、`--collect --remote` 或 `--status` 不启动 GPU。
所有命令从仓库根目录执行：

```powershell
# 只校验，不连接云端
uv run --no-sync python 002-pika-h3-flex/run.py --task tasks/004-pika-video-demo.json --enqueue --dry-run
# 独立提交每个镜头，云端服务需先部署并准备权重
uv run --no-sync python 002-pika-h3-flex/run.py --task tasks/004-pika-video-demo.json --enqueue
# 根据本地 outputs/.runner/jobs/pika.json 收集
uv run --no-sync python 002-pika-h3-flex/run.py --collect
# 不依赖本地登记，直接按多个 Task 从 Volume 下载
uv run --no-sync python 002-pika-h3-flex/run.py --collect --remote --task tasks/004-pika-video-demo.json --task tasks/004-pika-frames-demo.json
```

两种收集模式均复用 `ThreadPoolExecutor(max_workers=8)`，同一条命令内跨 Task
最多并发下载八个 MP4。使用 SHA-256 校验、镜头级文件锁和原子发布；
原始 Task（包括首尾帧文件）、模型配置及云端缓存指纹必须与生成时一致。
这只是下载并发，与 GPU 视频生成并发是独立配置；没有实现 MP4 的分片断点续传。
实际 Volume 检查发现当前缺少 `/009-pika-h3-flex/model-ready.json`，
须先验证或迁移 CPU 模型缓存；截至本次修改，尚未完成真实视频下载验收。
