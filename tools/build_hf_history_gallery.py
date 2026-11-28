"""Build an offline, paginated HF media gallery from verified migration receipts."""
from __future__ import annotations

import argparse
import base64
import io
import json
from pathlib import Path

from PIL import Image, ImageOps

from runtime.archive.huggingface import public_url
from runtime.utils.files import atomic_write, sha256_file

TEMPLATE = Path(__file__).with_name("hf_history_gallery.html")


def preview(path):
    with Image.open(path) as image:
        image.draft("RGB", (420, 280))
        image = ImageOps.exif_transpose(image)
        image.thumbnail((420, 280), Image.Resampling.LANCZOS)
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGBA" if "transparency" in image.info else "RGB")
        buf = io.BytesIO()
        image.save(buf, "WEBP", quality=70, method=2)
        return "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def previous_thumbnails(output):
    if not output.is_file():
        return {}
    try:
        raw = output.read_text(encoding="utf-8").split('id="archive-data">', 1)[1].split("</script>", 1)[0]
        return {a["original_sha256"]: a["thumb"] for a in json.loads(raw)["assets"] if a.get("thumb")}
    except (ValueError, IndexError, KeyError):
        return {}


def build(root, receipts_path, output):
    root = Path(root).resolve()
    output = Path(output)
    receipts = json.loads(Path(receipts_path).read_text(encoding="utf-8"))
    thumbs = previous_thumbnails(output)
    assets = []
    buckets = set()
    for relative, row in sorted(receipts.items()):
        if row.get("status") != "archived" or row.get("verification") != "sha256":
            continue
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Receipt path escapes output root")
        if row["url"] != public_url(row["bucket"], row["path"]):
            raise ValueError("Invalid receipt URL")
        if row["kind"] not in {"image", "video"}:
            continue
        buckets.add(row["bucket"])
        digest = row["original_sha256"]
        thumb = thumbs.get(digest, "")
        if row["kind"] == "image" and not thumb and path.is_file() and sha256_file(path) == digest:
            try:
                thumb = preview(path)
            except (OSError, ValueError):
                pass
            if thumb:
                thumbs[digest] = thumb
        assets.append({**{k: row[k] for k in ("url", "path", "kind", "stored_bytes", "original_sha256", "stored_sha256")},
                       "relative": relative, "project": relative.split("/")[0],
                       "name": path.name, "thumb": thumb})
    if len(buckets) > 1:
        raise ValueError("Gallery receipts must use one bucket")
    # Video previews use matching first-frame thumbnails if present.
    by_relative = {a["relative"]: a for a in assets}
    for a in assets:
        if a["kind"] == "video" and not a["thumb"]:
            candidate = a["relative"].replace("_video.mp4", "_first.png")
            if candidate != a["relative"]:
                a["thumb"] = by_relative.get(candidate, {}).get("thumb", "")
    data = {"assets": assets, "bucket_url": "https://huggingface.co/buckets/" + next(iter(buckets)) if buckets else "https://huggingface.co/"}
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    template = TEMPLATE.read_text(encoding="utf-8")
    atomic_write(output, template.replace("__DATA__", payload).encode("utf-8"))
    return len(assets), sum(bool(a["thumb"]) for a in assets), output.stat().st_size


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("outputs"))
    parser.add_argument("--receipts", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    receipts = args.receipts or args.root / ".runtime/history-hf-migration/receipts.json"
    output = args.output or args.root / "hf-gallery.html"
    count, thumbs, size = build(args.root, receipts, output)
    print(f"Gallery: {count} assets, {thumbs} previews, {size} bytes -> {output}")


if __name__ == "__main__":
    main()
