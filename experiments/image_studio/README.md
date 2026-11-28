# 私人图像工作台（实验版）

一句话生成 → 选任意版本 → 输入修改要求 → 保存新版本。也可直接拖入 PNG、JPEG 或 WebP；上传会去除 EXIF 等附加信息，推理失败不会覆盖源图。默认 16:9，也可以选 1:1、9:16；编辑按参考图尺寸请求输出。

## 与现有项目的关系

- 沿用现有 MiniMax H3 Image SGLang 配置和模型缓存，通过正式 SDK 生图与改图，不在 Web 进程加载模型。
- 正式 MiniMax H3 Image 模型 App 同时提供生图和改图接口，工作台使用正式 SDK 调用与持久化提交记录，不另设 HTTP 推理实现。
- 浏览器端先拿任务号再轮询，GPU 推理与页面连接分开；任务、原始上传和私有索引保存在独立的私有 Modal Volume，生成资产直接引用正式模型 Volume 的 private-studio 命名空间。本设备近期历史列表存在浏览器里。预览暂不写入现有 HF 画廊，不公开私人原图，也不自动删除。
- 第一版只做单张指令改图，不承诺遮罩外像素不变；没有跨设备项目列表、区域遮罩及多用户。派发结果不确定时不自动重交；如果排队或渲染长时间未更新，界面显示超时。中文指令及保真度需要真机图像回归；目前没有额外 LLM 自动改写指令。

## CPU 验证（不用 Modal 账户、GPU 或模型权重）

从仓库根目录在可写 Python 环境运行：

```sh
uv pip install 'fastapi>=0.115,<1' 'httpx>=0.28,<1' 'pillow>=11,<14'
python -m unittest experiments.image_studio.test_image_studio_preview -v
python -m unittest discover -s tests -p 'test_generation_gateway.py'
```

测试使用临时存储和模拟引擎，覆盖结果分支、原图字节保留、上传、出错、授权和网关的默认关闭行为。

## Modal 私人预览部署

1. 在同一 Modal 工作区使用 `python run.py prepare minimax --retry` 部署正式 App `005-minimax-h3-image`。SDK 会验证版本与模型缓存身份。
2. 创建 Secret `image-studio-preview`，设置独立强密码 `IMAGE_STUDIO_PASSWORD`。不再需要模型 Web URL 或 Proxy Token。不要把密码加入 Git 或发在聊天里。
3. `uv run python -m modal deploy experiments/image_studio/deploy.py` 后打开输出的 HTTPS URL。登录用户名是 `studio`，密码是步骤 2 设的值。不要共享该用户名和密码。先测一张图和连续两次小范围修改，再接历史画廊。
