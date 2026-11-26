"""Generate Modal Studio from existing task snapshots and verified archive receipts."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit

from PIL import Image

from runtime.archive.huggingface import public_url
from runtime.archive.preview import preview_url, verified_preview
from runtime.utils.files import atomic_write, sha256_file

TEMPLATE = Path(__file__).with_name("studio_dist") / "index.html"
RECEIPTS = Path(".runtime/history-hf-migration/receipts.json")
MEDIA = {".png", ".jpg", ".jpeg", ".webp", ".mp4"}


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def payload_from_html(path, name):
    try:
        text = path.read_text(encoding="utf-8-sig")
        raw = text.split('id="' + name + '">', 1)[1].split("</script>", 1)[0]
        return json.loads(raw)
    except (OSError, IndexError, ValueError):
        return {}


def read_feed(path):
    try:
        text = path.read_text(encoding="utf-8-sig").strip()
        prefix = "window.updateTaskMonitor("
        if not text.startswith(prefix) or not text.endswith(");"):
            return None
        value = json.loads(text[len(prefix):-2])
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def safe_url(url):
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme == "https" and parsed.netloc == "huggingface.co" and parsed.path.startswith("/buckets/"):
        return url
    if parsed.scheme or parsed.netloc or parsed.path.startswith("/") or "\\" in parsed.path:
        return None
    decoded = unquote(parsed.path)
    if "\\" in decoded or ":" in decoded:
        return None
    path = PurePosixPath(decoded)
    if path.is_absolute() or ".." in path.parts or path.suffix.lower() not in MEDIA:
        return None
    return url


def titles(root, project):
    value = read_json(root.parent / "tasks" / (project + ".titles.json"), [])
    return {str(x["id"]): x.get("title") for x in value if isinstance(x, dict) and "id" in x}


def remote_digest(url):
    if not url.startswith("https://huggingface.co/buckets/"):
        return None
    digest = PurePosixPath(urlsplit(url).path).stem
    return digest if re.fullmatch(r"[0-9a-f]{64}", digest) else None


def collect(root, previous=None, *, thumbs=True):
    root = Path(root).resolve()
    previous = previous or {}
    old = {a.get("digest") or a.get("original_sha256"): a
           for a in previous.get("assets", []) if a.get("thumb") and not a["thumb"].startswith("data:")}
    # Reuse the former gallery's verified thumbnail cache when available.
    for a in payload_from_html(root / "hf-gallery.html", "archive-data").get("assets", []):
        if a.get("thumb") and a.get("original_sha256"):
            old.setdefault(a["original_sha256"], a)

    preview_index = read_json(root / ".runtime/preview-index.json", {}) or {}
    receipts = read_json(root / RECEIPTS, {})
    # Direct-render projects predate the global receipt inventory. Their archive
    # records carry the same verified bucket path, stored size, and SHA-256.
    for result_path in sorted(root.glob("*-direct-results.json")):
        project = result_path.name.removesuffix("-direct-results.json")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", project):
            continue
        records = read_json(result_path, {})
        if not isinstance(records, dict):
            continue
        for shot, record in records.items():
            if not re.fullmatch(r"[0-9]+", shot) or not isinstance(record, dict):
                continue
            archive = record.get("archive")
            if not isinstance(archive, dict) or archive.get("verification") != "sha256":
                continue
            suffix = PurePosixPath(archive.get("path", "")).suffix.lower()
            if suffix not in MEDIA or record.get("sha256") not in {archive.get("stored_sha256"), archive.get("original_sha256")}:
                continue
            # The local direct result is a copy of the stored (encoded) media.
            receipts.setdefault(project + "/" + shot + suffix,
                                {**archive, "original_sha256": archive["stored_sha256"]})
    assets, canonical, buckets = [], {}, set()
    projects = {}
    for rel, row in sorted(receipts.items()):
        if row.get("status") != "archived" or row.get("verification") != "sha256":
            continue
        try:
            path = (root / rel).resolve()
            if not path.is_relative_to(root):
                continue
            href = public_url(row["bucket"], row["path"])
            if row.get("url") != href or row["kind"] not in {"image", "video"}:
                continue
            digest = row["original_sha256"]
            project = rel.split("/")[0]
            local = quote(rel)
            cached = old.get(digest, {})
            thumb, dims = verified_preview(row, preview_index), cached.get("dimensions")
            if thumbs and row["kind"] == "image" and path.is_file():
                if not dims:
                    try:
                        with Image.open(path) as image:
                            dims = list(image.size)
                    except (OSError, ValueError):
                        pass
            if not thumb and row["kind"] == "image":
                thumb = href
            title_map = projects.setdefault(project, titles(root, project))
            title = title_map.get(path.stem.split("_")[0])
            item = {"id": rel, "project": project, "name": path.name,
                    "title": title or path.stem.replace("_", " "), "kind": row["kind"],
                    "url": href, "local_url": local, "bytes": row["stored_bytes"],
                    "digest": digest, "thumb": thumb, "dimensions": dims, "storage": "cloud"}
            assets.append(item)
            canonical[local] = item
            buckets.add(row["bucket"])
        except (KeyError, TypeError, ValueError, OSError):
            continue

    tasks = []
    runtime = root.parent / ".runtime" / "monitor"
    snapshots = {}
    for path in sorted(runtime.glob("*.js")):
        data = read_feed(path)
        if data and data.get("name") == path.stem:
            snapshots[path.stem] = (data, quote("../.runtime/monitor/" + path.name))
    legacy = {}
    for path in sorted([*root.glob("监控*.html"), *root.glob("*/监控*.html")]):
        if ".runtime" in path.relative_to(root).parts or not path.name.startswith("监控"):
            continue
        # Only read historical HTML if a task feed does not already exist.
        name = path.stem[2:]
        legacy.setdefault(name, quote(path.relative_to(root).as_posix()))
        if name not in snapshots:
            data = payload_from_html(path, "initial-data")
            if data.get("name") == name:
                snapshots[name] = (data, None)
    for name, (snapshot, feed) in sorted(snapshots.items(), reverse=True):
        if name not in legacy:
            continue
        title_map = projects.setdefault(name, titles(root, name))
        summary_fields = ("name", "task_id", "status", "status_label", "counts", "total",
                          "downloaded", "updated_at", "elapsed_ms", "started_at", "finished_at")
        task_snapshot = ({key: snapshot[key] for key in summary_fields if key in snapshot}
                         if feed else snapshot)
        tasks.append({"id": name, "feed": feed, "snapshot": task_snapshot,
                      "legacy": legacy.get(name)})
        # Keep full job details in the separate monitor feed. The gallery only
        # needs a task summary; asset prompts are copied below for search/viewer.
        for job in snapshot.get("jobs", []):
            a = job.get("asset")
            if not a:
                continue
            url = safe_url(a.get("url"))
            if not url:
                job["asset"] = None
                continue
            local_key = a.get("local_url") or url
            if local_key in canonical:
                item = canonical[local_key]
                job["asset"] = {**a, "url": item["url"], "thumb": item["thumb"],
                                "dimensions": a.get("dimensions") or item.get("dimensions")}
                if job.get("prompt"):
                    item["prompt"] = job["prompt"]
                if title_map.get(job.get("item")):
                    item["title"] = title_map[job["item"]]
                continue
            kind = a.get("kind")
            if kind not in {"image", "video"}:
                continue
            local = not url.startswith("https://")
            if not local:
                parts = urlsplit(url).path.split("/")
                if len(parts) >= 5:
                    buckets.add(parts[2] + "/" + parts[3])
            path = (root / unquote(urlsplit(url).path)).resolve() if local else None
            thumb = (safe_url(a.get("thumb")) or "") if local else ""
            if not local:
                if remote_digest(url):
                    parts = urlsplit(url).path.split("/")
                    thumb = verified_preview({"url": url, "stored_sha256": remote_digest(url), "bucket": "/".join(parts[2:4])}, preview_index)
                elif thumb.startswith("https://huggingface.co/buckets/"):
                    pass
                else:
                    thumb = ""
            dims = a.get("dimensions")
            digest = remote_digest(url) if not local else None
            if digest and digest in old:
                dims = dims or old[digest].get("dimensions")
            if local and kind == "image" and path.is_relative_to(root) and path.is_file():
                digest = sha256_file(path)
                cached = old.get(digest, {})
                thumb, dims = cached.get("thumb", ""), dims or cached.get("dimensions")
                if not thumb:
                    thumb = quote(path.relative_to(root).as_posix())
            if not local and not thumb and kind == "image":
                thumb = url
            filename = a.get("filename") or job["key"]
            if re.fullmatch(r"[0-9a-f]{64}\.[A-Za-z0-9]+", filename):
                filename = str(job["item"]) + "_" + job["step"] + PurePosixPath(filename).suffix
            assets.append({"id": name + "/" + job["key"], "project": name,
                           "name": filename, "title": title_map.get(job.get("item")) or job.get("item"),
                           "kind": kind, "url": url, "bytes": a.get("bytes"),
                           "thumb": thumb, "dimensions": dims, "digest": digest,
                           "prompt": job.get("prompt"), "storage": "local" if local else "cloud"})
    by_id = {a["id"]: a for a in assets}
    for item in assets:
        if item["kind"] == "video" and not item["thumb"]:
            candidate = item["id"].replace("_video.mp4", "_first.png")
            if candidate != item["id"]:
                item["thumb"] = by_id.get(candidate, {}).get("thumb", "")
    from orchestration.gallery_derivatives import INDEX, apply_derivatives
    apply_derivatives(assets, read_json(root / INDEX, {}) or {}, root)
    return {"assets": assets, "tasks": tasks,
            "bucket_url": "https://huggingface.co/buckets/" + sorted(buckets)[0] if buckets else None}



def publish_catalog(root, snapshots):
    """Update a small local catalogue without encoding images or reading cloud media."""
    root = Path(root)
    tasks = []
    for snapshot in snapshots:
        name = snapshot["name"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            continue
        data = {k: snapshot.get(k) for k in
                ("name", "task_id", "status", "status_label", "counts", "total",
                 "downloaded", "updated_at", "elapsed_ms")}
        # Keep only enough media information for the gallery; the selected task
        # loads the full existing monitor feed on demand.
        data["jobs"] = [{"key": j["key"], "item": j["item"], "step": j["step"],
                         "prompt": j.get("prompt"), "asset": j["asset"]} for j in snapshot.get("jobs", [])
                        if j.get("asset") and safe_url(j["asset"].get("url"))]
        tasks.append({"id": name, "snapshot": data,
                      "feed": quote("../.runtime/monitor/" + name + ".js"),
                      "legacy": quote("监控" + name + ".html")})
    payload = json.dumps(tasks, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    path = root / ".runtime" / "studio-catalog.js"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, ("window.updateStudioCatalog(" + payload + ");").encode("utf-8"))


def render(root, *, output=None, view="gallery", selected=None, thumbs=True):
    root = Path(root).resolve()
    output = Path(output or root / "index.html")
    if not TEMPLATE.is_file():
        raise RuntimeError("Build Studio first: cd frontend && npm ci && npm run build")
    previous = payload_from_html(output, "studio-data")
    data = collect(root, previous, thumbs=thumbs)
    data.update(view=view, selected=selected,
                catalog_url=".runtime/studio-catalog.js" if (root / ".runtime/studio-catalog.js").is_file() else None)
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    template = TEMPLATE.read_text(encoding="utf-8")
    if template.count("__STUDIO_DATA__") != 1:
        raise RuntimeError("Invalid Studio build template")
    atomic_write(output, template.replace("__STUDIO_DATA__", payload).encode("utf-8"))
    return data


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path("outputs"))
    p.add_argument("--output", type=Path)
    p.add_argument("--view", choices=("gallery", "monitor"), default="gallery")
    p.add_argument("--selected")
    args = p.parse_args()
    data = render(args.root, output=args.output, view=args.view, selected=args.selected)
    print(f"Studio: {len(data['assets'])} media, {len(data['tasks'])} task records, "
          f"{sum(bool(a['thumb']) for a in data['assets'])} preview references")


if __name__ == "__main__":
    main()
