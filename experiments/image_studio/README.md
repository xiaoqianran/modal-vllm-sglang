# 私人图像工作台（实验版）

一句话生成 → 选任意版本 → 输入修改要求 → 保存新版本。也可直接拖入 PNG、JPEG 或 WebP；上传会去除 EXIF 等附加信息，推理失败不会覆盖源图。默认 16:9，也可以选 1:1、9:16；编辑按参考图尺寸请求输出。

## 与现有项目的关系

- 沿用现有 MiniMax H3 Image SGLang 配置和模型缓存，验证模型官方的 JSON 生图及 multipart 改图接口，不在 Web 进程加载模型。
- 网关透传改图是显式开关，旧生图和旧视频服务默认不受影响。实验模型 App 与生产 App 名称不同；复用缓存前须让预览部署指向有该版本权重的同一模型 Volume/命名空间。
- 浏览器端先拿任务号再轮询，GPU 推理与页面连接分开；图片和任务保存在独立的私有 Modal Volume。本设备近期历史列表存在浏览器里。预览暂不写入现有 HF 画廊，不公开私人原图，也不自动删除。
- 第一版只做单张指令改图，不承诺遮罩外像素不变；没有跨设备项目列表、区域遮罩及多用户。派发结果不确定时不自动重交；如果排队或渲染长时间未更新，界面显示超时。中文指令及保真度需要真机图像回归；目前没有额外 LLM 自动改写指令。

## CPU 验证（不用 Modal 账户、GPU 或模型权重）

从仓库根目录在可写 Python 环境运行：

```sh
uv pip install 'fastapi>=0.115,<1' 'httpx>=0.28,<1' 'pillow>=11,<14'
python -m unittest experiments.image_studio.test_studio -v
python -m unittest discover -s tests -p 'test_generation_gateway.py'
```

测试使用临时存储和模拟引擎，覆盖结果分支、原图字节保留、上传、出错、授权和网关的默认关闭行为。

## Modal 私人预览部署

1. 用现有项目的 Modal 环境运行 `uv run python -m modal deploy experiments/image_studio/deploy_engine.py`。确认目标是新 App `image-studio-h3-preview`，不是生产模型；部署和首次推理可能计费。
2. 在目标 Modal 环境创建 Secret `image-studio-preview`。填写独立强密码 `IMAGE_STUDIO_PASSWORD`、步骤 1 的真实 Modal Web URL `IMAGE_STUDIO_H3_URL`，以及该 Web 端点要求的 Proxy Token `IMAGE_STUDIO_H3_MODAL_KEY` 和 `IMAGE_STUDIO_H3_MODAL_SECRET`。不要把这些值加到 Git 或发在聊天里。
3. `uv run python -m modal deploy experiments/image_studio/deploy.py` 后打开输出的 HTTPS URL。登录用户名是 `studio`，密码是步骤 2 设的值。不要共享该用户名和密码。先测一张图和连续两次小范围修改，再接历史画廊。
