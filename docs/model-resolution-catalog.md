# Model Generation Sizes and Resolutions

Research date: 2026-11-18.

This catalog separates upstream published capabilities from settings and validation rules currently exposed by this repository. A dimension accepted by validation is not evidence of output quality, memory fit, or successful GPU execution.

## Summary

| Model | Upstream information | Repository default and accepted dimensions | Validation status |
| --- | --- | --- | --- |
| MOSS-Image-SFT | Model card describes image generation but does not establish definitive hard canvas bounds for this checkpoint. | Default `1024x1024`; positive width and height divisible by 16. Shared presets cover long edges 256, 512, 768, 1024, 2048, 4096 at five aspect ratios. | 2K/4K parameter-validated offline only; no GPU quality or memory acceptance. |
| Pika H3 Flex | Upstream material references 480p and 768p modes. Published bucket dimensions may differ from repository mapping. | 480p: 864x480 (16:9), 480x864 (9:16), 640x480 (4:3), 480x640 (3:4), 480x480 (1:1). 768p: 1344x768 (16:9 only). | Mapping declared by `models/002-pika-h3-flex/model.json`. See [Pika performance acceptance](pika-performance.md) for actual generation evidence. |
| MiniMax H3 Image | Official materials describe a typical 4-20 MP range and common aspect ratios; guidance, not the repository validator lower bound. | Default `2048x2048`. Validator accepts 32-aligned dimensions with area 1-20 MP. | Request validation only; not proof of native generation or quality. |

## Shared Image Presets

`runtime/config/resolution.py` defines project presets. Dimensions are calculated from long edge and aspect ratio:

| Long edge | 16:9 | 9:16 | 4:3 | 3:4 | 1:1 |
| --- | --- | --- | --- | --- | --- |
| 256 | 256x144 | 144x256 | 256x192 | 192x256 | 256x256 |
| 512 | 512x288 | 288x512 | 512x384 | 384x512 | 512x512 |
| 768 | 768x432 | 432x768 | 768x576 | 576x768 | 768x768 |
| 1K | 1024x576 | 576x1024 | 1024x768 | 768x1024 | 1024x1024 |
| 2K | 2048x1152 | 1152x2048 | 2048x1536 | 1536x2048 | 2048x2048 |
| 4K | 4096x2304 | 2304x4096 | 4096x3072 | 3072x4096 | 4096x4096 |

Shared presets do not mean every model accepts every entry. MOSS applies 16-pixel alignment. MiniMax applies 32-pixel alignment and the 1-20 MP area check, so many small presets fail. Engine constraints can be stricter.

## Upstream Sources

- MOSS-Image-SFT model card: https://huggingface.co/OpenMOSS-Team/MOSS-Image-SFT
- OpenMOSS image project: https://github.com/OpenMOSS-Team/MOSS-Image
- Pika H3 Flex model card: https://huggingface.co/Pika-Labs/Pika-H3-Flex
- MiniMax H3 Image model card: https://huggingface.co/MiniMaxAI/MiniMax-H3-Image
- MiniMax H3 Image project: https://github.com/MiniMax-AI/MiniMax-H3

Repository facts were checked against each model's `model.json`, model-owned `model_impl/params.py` or `model_impl/params.py`, and `runtime/config/resolution.py` on the research date. The upstream Pika and repository 16:9 canvases are not assumed equivalent; local 864x480 is a configuration choice until confirmed by a real generation run.
