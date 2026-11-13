# 新增模型

1. 建立模型目录，保留 app.py、download_model.py、model_cache.py、pyproject.toml、README.md。
2. 核对官方模型、依赖组件、引擎和启动命令。CPU 下载，GPU 只读校验。锁定具体镜像和权重版本。
3. 在 orchestration/adapters.py 现有能力表和参数函数加入模型；协议相同就复用 shared/inference_api.py。
4. 在 endpoints.example.json 添加端点环境变量，在 workflows/、tasks/ 各加一个最小示例。
5. 跑离线测试和 plan；部署后验证一张图和 resume 复用。组合工作流另做真实验收。

新增模型不改 runner.py / state.py，不复制批处理和恢复逻辑。实际出现重复才抽共享代码。
003-minimax-h3-image 是当前例子：仅增加文生图；图片编辑、参考图列表、提示词扩写尚未接入。
图片协议复用 VLLMImageClient，保留历史类名兼容已有脚本。
升级影响生成结果的配置后更新服务 revision，并建立新场次任务。
