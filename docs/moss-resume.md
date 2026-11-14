# MOSS 统一 SDK 恢复

MOSS 与 Pika、MiniMax 使用相同的公共执行器，见 [根目录说明](../README.md#4-结果与恢复)。
从仓库根目录指定一个或多个 task：

```powershell
uv run python 001-moss-image/run.py --task tasks/003-stop-rain-001-rain-bus-stop.json --dry-run
uv run python 001-moss-image/run.py --task tasks/003-stop-rain-001-rain-bus-stop.json
```

重复原命令会校验本地结果；缺失文件先读取云端 Volume，完成记录包含请求与模型缓存指纹、SHA256。
每张图片在云端提交后立即流式通知客户端，不必等待整批完成。
客户端中断后不改变 task，重新执行同一命令即可恢复。
同步图片响应丢失且没有可验证的已保存文件时，停止自动重提，避免重复生成。
