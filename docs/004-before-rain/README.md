# 004 分镜自动生成

`storyboard.json` 是唯一的可编辑分镜源数据。不要直接修改衍生的 `图片提示词.md`、`视频提示词.md`、`分镜.md` 或 `image-prompts.json`：它们会在重新生成时同步更新。

从仓库根目录执行：

```powershell
# 只检查文件和源数据是否一致，不修改任何文件
uv run --no-sync python scripts/generate_storyboard.py --check

# 根据源数据更新五个输出（需要显式允许覆盖）
uv run --no-sync python scripts/generate_storyboard.py --force
```

无 `--force` 时，仅创建不存在的文件；如已存在的输出与源数据不同，命令报错而不覆盖。

## 复用到其他故事

先复制 `storyboard.json` 到例如 `docs/005-another-story/storyboard.json`，修改以下字段：

- `name`：小写连字符 Task 名；输出为 `tasks/<name>.json`。
- `title`、`style`、`female`、`male`、`avoid`：固定故事风格和人物特征。
- `params`：Pika FL2VA 时长、六步采样和画幅参数。
- `input_dir`：输入首尾帧路径，默认 `inputs/pika`。
- `shots`：每镜 `id`、`title`、`location`、`first`、`last`、`motion`、`soundscape`、`music` 和可选 `seed`。

然后：

```powershell
uv run --no-sync python scripts/generate_storyboard.py --source docs/005-another-story/storyboard.json
```

本工具仅支持当前项目的 `pika-fl2va`（首尾帧视频），不调用 GPT Image、MOSS 或 Pika API，也不会创建真正的图片文件或消耗 GPU。生成后的首尾帧文件应与 `inputs/pika/001_first.png`、`001_last.png` 等引用一一对应；实际生成视频前需先补齐图片。

视频提示词使用 H3 风格的创作段落结构，不能将其误认作 Pika 官方保证支持的格式。
