"""从结构化分镜生成图片、视频提示词文档。

当前 tasks/004-pika-frames-demo.json 是独立维护的多模型任务，不由本脚本覆盖。
不调用图片/视频 API，不读取密钥，不提交 GPU 任务。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = ROOT / "docs/004-before-rain/storyboard.json"

ALIGNMENT = (
    "How the reference pictures align with the target video — "
    "Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; "
    "Picture 2 (from Shot 1) aligns with the {duration:.2f}-second mark of the target video."
)


def _require_text(data: dict, name: str, owner: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{owner}.{name} must be nonempty text")
    return value.strip()


def validate_storyboard(source: dict) -> dict:
    """只接受显式定义的分镜字段，不推测缺失镜头。"""
    if not isinstance(source, dict):
        raise ValueError("storyboard must be an object")
    for field in ("name", "title", "workflow", "style", "female", "male", "avoid"):
        _require_text(source, field, "storyboard")
    if source["workflow"] != "pika-fl2va":
        raise ValueError("Only pika-fl2va is currently supported")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", source["name"]):
        raise ValueError("name must be a lowercase hyphenated identifier")
    if type(source.get("version")) is not int or source["version"] != 1:
        raise ValueError("version must be 1")
    target = source.get("params")
    if not isinstance(target, dict):
        raise ValueError("params must be an object")
    seconds = target.get("seconds")
    if type(seconds) is not int or not 5 <= seconds <= 15:
        raise ValueError("Pika duration must be 5–15 whole seconds")
    if target.get("num_inference_steps") != 6 or type(target.get("num_inference_steps")) is not int:
        raise ValueError("This model configuration requires six inference steps")
    dimensions = target.get("target")
    if (not isinstance(dimensions, dict) or
        set(dimensions) != {"short_edge", "aspect_ratio", "duration_seconds"} or
        type(dimensions["short_edge"]) is not int or dimensions["short_edge"] <= 0 or
        not isinstance(dimensions["aspect_ratio"], str) or
        not re.fullmatch(r"[1-9]\d*:[1-9]\d*", dimensions["aspect_ratio"]) or
        type(dimensions["duration_seconds"]) not in (int, float) or
        dimensions["duration_seconds"] != seconds):
        raise ValueError("Invalid video target or duration mismatch")
    input_dir = source.get("input_dir", "inputs/pika")
    if (not isinstance(input_dir, str) or not input_dir or
        Path(input_dir).is_absolute() or "\\" in input_dir or
        any(part in ("", ".", "..") for part in input_dir.split("/"))):
        raise ValueError("input_dir must be a safe repository-relative directory")
    shots = source.get("shots")
    if not isinstance(shots, list) or not shots:
        raise ValueError("shots must be a nonempty array")
    seen = set()
    for shot in shots:
        if not isinstance(shot, dict):
            raise ValueError("Each shot must be an object")
        sid = _require_text(shot, "id", "shot")
        if not re.fullmatch(r"\d{3}", sid) or sid in seen:
            raise ValueError("shot.id must be unique three-digit text")
        seen.add(sid)
        for name in ("title", "location", "first", "last", "motion", "soundscape", "music"):
            _require_text(shot, name, sid)
        if "seed" in shot and (type(shot["seed"]) is not int or not 0 <= shot["seed"] < 2**32):
            raise ValueError(f"{sid}.seed must be uint32")
    return source


def duration_word(seconds: int) -> str:
    return {5: 'five'}.get(seconds, str(seconds))

def image_caption(source: dict, shot: dict, phase: str) -> str:
    state = shot["first" if phase == "first" else "last"]
    anchor = "OPENING FRAME AT 0.00 SECONDS" if phase == "first" else (
        f"ENDING FRAME AT {source['params']['seconds']:.2f} SECONDS"
    )
    return (
        f"{source['style']}.\n\n"
        f"CAMERA AND SETTING: {shot['location']}.\n\n"
        f"SUBJECTS: {source['female']}. {source['male']}.\n\n"
        f"{anchor}: {state}.\n\n"
        f"This is a single keyframe for a continuous {duration_word(source['params']['seconds'])}-second shot. "
        "Preserve identical character identity, faces, clothes, lens, left-right positions, "
        "architecture, light direction, weather, notebook and umbrella count between this pair. "
        f"{source['avoid']}."
    )


def video_prompt(source: dict, shot: dict) -> str:
    seconds = source["params"]["seconds"]
    return (
        ALIGNMENT.format(duration=seconds)
        + "\n\nintegrated_multimodal_description: [Shot 1] "
        "Photorealistic live-action 16:9 romantic drama, one uninterrupted "
        f"{duration_word(seconds)}-second take. Begin with the exact face identities, costumes, staging, "
        f"lighting, camera and props in Picture 1. {shot['motion']}. "
        "All intervening gestures unfold gradually and physically plausibly, landing "
        "precisely on the pose, positions, gaze, prop states and composition of Picture 2 "
        f"at {seconds:.2f} seconds. Lin retains the straight black bob, LEFT silver crescent "
        "clip, pale-blue cardigan and navy notebook; Chen retains his green varsity jacket "
        "and the SAME SINGLE transparent umbrella. No hard cuts, morphs, abrupt camera "
        "changes, wardrobe changes or extra characters.\n\n"
        f"overall_soundscape: {shot['soundscape']}.\n\n"
        f"non_diegetic_music: {shot['music']}."
    )


def render(source: dict, *, story_dir: str, input_dir: str) -> dict[str, str]:
    """纯渲染函数；不做任何文件系统写操作。"""
    validate_storyboard(source)
    story_dir = story_dir.rstrip("/")
    input_dir = input_dir.rstrip("/")
    story = [
        f"# {source['title']}｜最终分镜", "",
        f"{len(source['shots'])}组镜头，每镜{source['params']['seconds']}秒；"
        "16:9；Pika H3 Flex FL2VA；首尾帧应先完成一致性验收。", "",
        f"固定女主：{source['female']}",
        f"固定男主：{source['male']}", "",
        "## 分镜列表", "",
    ]
    images = [
        "# GPT Image 与 MOSS Image 图片提示词", "",
        "GPT Image 使用场景、人物、构图、光线、画面状态与固定约束；"
        "MOSS 使用这些英语段落作为 caption 正向提示词。"
        "先生成每镜 FIRST，再以它为视觉参考编辑 LAST。", "",
    ]
    videos = [
        "# Pika H3 Flex FL2VA 视频提示词", "",
        "使用 H3 风格的运动、声场和配乐段落辅助创作；"
        "实际通过 first_frame 和 last_frame 传入两张图片，"
        "不把该提示词格式视为 Pika 官方 API 保证。", "",
    ]
    manifest = []
    for shot in source["shots"]:
        sid = shot["id"]
        story.extend([
            f"## 镜头 {sid}｜{shot['title']}", "",
            f"场景摄影：{shot['location']}", "",
            f"第一帧：{shot['first']}", "",
            f"最后一帧：{shot['last']}", "",
            f"{source['params']['seconds']}秒连续运动：{shot['motion']}", "",
        ])
        for phase in ("first", "last"):
            frame = f"{input_dir}/{sid}_{phase}.png"
            caption = image_caption(source, shot, phase)
            manifest.append({"id": sid, "frame": phase, "file": frame, "caption": caption})
            images.extend([f"## {sid}_{phase}.png", "", frame, "", "PROMPT:", "", caption, ""])
        prompt = video_prompt(source, shot)
        videos.extend([
            f"## 镜头 {sid}｜{shot['title']}", "",
            f"INPUTS: {input_dir}/{sid}_first.png → {input_dir}/{sid}_last.png",
            "", prompt, "",
        ])
    return {
        f"{story_dir}/分镜.md": "\n".join(story).rstrip("\n") + "\n",
        f"{story_dir}/图片提示词.md": "\n".join(images).rstrip("\n") + "\n",
        f"{story_dir}/视频提示词.md": "\n".join(videos).rstrip("\n") + "\n",
        f"{story_dir}/image-prompts.json": json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    }


def _relative_dir(path: Path, root: Path) -> str:
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("Output directory must be inside repository root") from exc
    return rel.as_posix()


def generate(source_path: Path, *, root: Path = ROOT, check: bool = False,
             force: bool = False, output_dir: Path | None = None) -> list[Path]:
    """默认保护现有文件。--check 只读比较；--force 才允许更新不同内容。"""
    if check and force:
        raise ValueError("--check and --force cannot be combined")
    source = validate_storyboard(json.loads(source_path.read_text(encoding="utf-8-sig")))
    folder = output_dir or source_path.parent
    output = render(
        source, story_dir=_relative_dir(folder, root),
        input_dir=source.get("input_dir", "inputs/pika"),
    )
    paths = []
    for rel, content in output.items():
        path = root / rel
        if path.exists():
            same = path.read_text(encoding="utf-8") == content
            if check and not same:
                raise ValueError(f"Out of date: {rel}")
            if not check and not force and not same:
                raise FileExistsError(f"Refusing to overwrite {rel}; use --force")
        elif check:
            raise FileNotFoundError(f"Missing generated file: {rel}")
        paths.append(path)
    if check:
        return paths
    for path, content in output.items():
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists() or destination.read_text(encoding="utf-8") != content:
            destination.write_text(content, encoding="utf-8")
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="分镜源 JSON")
    parser.add_argument("--output-dir", type=Path, help="Markdown 与图片提示词输出目录")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="只读检查生成内容与源数据一致")
    action.add_argument("--force", action="store_true", help="允许更新已有输出文件")
    args = parser.parse_args(argv)
    try:
        paths = generate(args.source, check=args.check, force=args.force,
                         output_dir=args.output_dir)
    except (ValueError, FileNotFoundError, FileExistsError, json.JSONDecodeError) as exc:
        parser.exit(2, f"错误：{exc}\n")
    print(("CHECK OK" if args.check else "GENERATED") + f": {len(paths)} files")
    for path in paths:
        print(path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
