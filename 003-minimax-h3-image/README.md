# MiniMax H3 Image / Modal / SGLang

CPU 准备权重，独立 Modal 服务，根目录编排器批量生图。
接入 minimax.t2i，默认 2048×2048、6 步、CPU seed generator，不发送 CFG 或负面提示词。
工作流允许尺寸为 32 的倍数、面积 4–20 MP，这是本项目的保守输入限制。

## 部署

```powershell
cd 003-minimax-h3-image
uv run modal run download_model.py
uv run modal run download_model.py --check-only
uv run modal deploy app.py
```

复用 modal-forge Volume 的独立 003-minimax-h3-image 缓存，不改已有模型缓存。
下载图像模型运行组件及 MiniMax-H3 的 text_encoder/，约 135 GB，不下载视频权重和 PE。
权重 revision 集中在 model_cache.py。GPU 校验清单后使用本地 snapshot 和显式编码器路径。
默认 RTX-PRO-6000、12 CPU、256 GiB 主内存、单容器单请求，120 秒空闲缩容。
MINIMAX_GPU 可覆盖 GPU；MINIMAX_IMAGE 可覆盖基础镜像。默认镜像固定 digest，SGLang 固定源码 commit。
源码在镜像构建时安装 diffusion 依赖，pip 间接依赖尚未完整锁定。轻量代码最后挂载。
采用默认组件放置策略、关闭启动预热，实际性能和资源占用需云端验收。

## 生成

回到仓库根目录，配置 deploy 输出的 URL 和 Modal Proxy Auth 凭据：

```powershell
cd ..
$env:MINIMAX_ENDPOINT = 'https://your-service.modal.run'
$env:MINIMAX_MODAL_KEY = '<Modal proxy key>'
$env:MINIMAX_MODAL_SECRET = '<Modal proxy secret>'
uv run python -m orchestration plan tasks/003-minimax-image-demo.json
uv run python -m orchestration run tasks/003-minimax-image-demo.json
uv run python -m orchestration resume outputs/003-minimax-image-demo/001-main
```

结果：outputs/003-minimax-image-demo/001-main/001.png。
任务内容在 tasks/003-minimax-image-demo.json；分辨率在 workflow 步骤中加 `"params": {"size": "4096x4096"}`。
新增镜头使用新的 item ID；resume 不重复生成已完成镜头。同步响应丢失保留 uncertain，不自动重提。

## 验收边界

本次没有下载大模型、部署服务或调用付费 GPU。离线测试不证明云端构建和推理成功。
上线顺序：CPU 下载检查 → 构建启动 → 示例一张图 → resume 复用 → 按需组合视频。
模型本身支持编辑，但本次工作流只接入文生图。

官方依据（2026-11-13 核对）：

- [模型和 API 参数](https://huggingface.co/MiniMaxAI/MiniMax-H3-Image)
- [SGLang 部署配方](https://github.com/sgl-project/sglang/blob/main/docs/cookbook/diffusion/MiniMax/MiniMax-H3-Image.mdx)

权重采用核对时实际可访问的 revision，不照搬配方的历史测量版本，也不继承其性能验收结论。
