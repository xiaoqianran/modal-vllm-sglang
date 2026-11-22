# 006-qwen38-pi

本目录通过 GitHub 仓库调用现有 Qwen3.8 SGLang Modal 部署，默认不依赖本机其他仓库目录。

GitHub: https://github.com/xiaoqianran/Qwen3.8-27B-RTX-6000-PRO-SGLang-DSpark

在 modal-vllm-sglang 根目录执行：

```powershell
# 安全预览（不连接 GitHub，不部署）
uv run --no-sync python 006-qwen38-pi/app.py deploy --dry-run

# 自动 GitHub 浅克隆至用户缓存，调用后端 Modal 部署
uv run --no-sync python 006-qwen38-pi/app.py deploy

# 主动同步 GitHub 最新代码后部署
uv run --no-sync python 006-qwen38-pi/app.py deploy --update-backend

# 测试未推送的本地修改（路径可任意）
uv run --no-sync python 006-qwen38-pi/app.py deploy --backend-path D:\somewhere\backend
```

默认克隆 main 到用户目录 .cache/qwen38-pi/backend，后续复用缓存；--update-backend 才会拉取最新代码，且拒绝覆盖本地未提交修改。--cache-dir 可修改缓存位置，--ref 可指定首次克隆的分支。真实部署会打印使用的 commit。

调用链：006/app.py -> GitHub 缓存 -> deploy/modal_app.py -> Modal Qwen38HTTP -> SGLang + RTX PRO 6000 + Pi LoRA。模型名称：qwen3.8-27b:qwen3.8-27B-pi。

默认设置 QWEN38_PI_LORA=1 和 QWEN38_SPECULATIVE_ALGORITHM=NONE；密钥、Volume、LoRA 权重由后端负责。原 download_model.py/model.json 暂保留，仅用于旧版模型缓存准备，不属于新部署入口。

注意：PC2 后端仓库当前存在未提交的改动，GitHub 版本不包含这些改动；需先推送，或以 --backend-path 指定本地版本进行测试。运行 Modal 会产生费用，--dry-run 不会部署。
