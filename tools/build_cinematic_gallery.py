"""Build a standalone film-contact-sheet HTML from verified Task/HF archive records.

Prefer verified local image copies when present; otherwise use HF archive URLs.
No model files are read or downloaded.
"""
import argparse
import hashlib
from html import escape
import json
from pathlib import Path
from urllib.parse import urlparse, quote

from orchestration import state
from orchestration.monitor_data import build_snapshot
from orchestration.monitor import dimensions
from runtime.utils.files import read_json, atomic_write

ROOT = Path(__file__).resolve().parents[1]
TASK_NAME = "022-minimax-cinematic-atlas-12"


def collect(directory, titles):
    plan = read_json(directory / "plan.json")
    current = read_json(directory / "state.json")
    snapshot = build_snapshot(directory, plan, current, dimensions)
    rows = {row["item"]: row for row in snapshot["jobs"] if row["step"] == "image"}
    output = []
    for item in titles:
        row = rows.get(item["id"])
        if row is None:
            raise ValueError(f"Missing expected image job {item['id']}")
        asset = row.get("asset") or {}
        url = asset.get("url")
        if url is not None:
            parsed = urlparse(url)
            if (parsed.scheme != "https" or parsed.hostname != "huggingface.co"
                    or not parsed.path.startswith("/buckets/")):
                raise ValueError("Only verified public HF Bucket image URLs are permitted")
        output.append({
            "id": item["id"], "title": item["title"], "en": item["en"],
            "prompt": row.get("prompt") or "", "status": row["status"],
            "url": url, "bytes": asset.get("bytes"),
        })
    return output


def prefer_local_images(items, folder, *, relative_prefix=""):
    """Use a local archive copy only when its content-addressed HF hash matches."""
    folder = Path(folder)
    for item in items:
        url = item.get("url")
        if not url:
            continue
        shot = item["id"]
        if not isinstance(shot, str) or not shot.isascii() or not shot.isdigit():
            continue
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "huggingface.co" or not parsed.path.startswith("/buckets/"):
            continue
        remote = Path(parsed.path).name
        digest = Path(remote).stem
        suffix = Path(remote).suffix.lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest) or suffix not in {".webp", ".png", ".jpg", ".jpeg"}:
            continue
        for filename in (shot + suffix, remote):
            local = folder / filename
            if local.is_file() and hashlib.sha256(local.read_bytes()).hexdigest() == digest:
                item["local_src"] = relative_prefix + quote(filename)
                break
    return items


def render_gallery(items):
    count = sum(bool(x["url"]) for x in items)
    headings = (
        '<header class="mast"><div class="mast-left"><span class="series">FRAME ARCHIVE / VOL. 01</span>'
        '<h1>Cinematic<br><em>Atlas.</em></h1><p>十二个独立电影画面。由 MiniMax H3 Image 在 Modal GPU 生成，原图直接存放于 Hugging Face Bucket。</p>'
        '</div><div class="mast-right"><span class="big-count">'+str(count).zfill(2)+'<i>/12</i></span>'
        '<span class="counter-note">已归档画面</span><span class="submeta">16:9 &nbsp; / &nbsp; MiniMax H3 Image &nbsp; / &nbsp; 2K</span></div></header>'
    )
    cards=[]
    for n,item in enumerate(items):
        title, en, prompt = (escape(str(item[k]), quote=True) for k in ("title","en","prompt"))
        number=escape(item["id"],quote=True)
        url=item["url"]
        if url:
            safe=escape(url,quote=True)
            source=escape(item.get("local_src") or url,quote=True)
            art=(f'<a class="visual" href="{source}" target="_blank" rel="noopener noreferrer" aria-label="打开 {title} 原图">'
                 f'<img src="{source}" alt="{title} · {en}" loading="lazy" referrerpolicy="no-referrer"><span class="open">查看原图 ↗</span></a>')
            status='<span class="state done">已发布</span>'
            if item.get("local_src"):
                status='<span class="state done">本地已校验 · <a href="'+safe+'" target="_blank" rel="noopener noreferrer">HF</a></span>'
        else:
            art='<div class="visual placeholder"><span>AWAITING FRAME</span></div>'
            status='<span class="state">尚未发布</span>'
        cards.append(f'<article class="frame"><div class="frame-no">{number} / {str(n+1).zfill(2)}</div>{art}'
                     f'<div class="frame-details"><div><h2>{title}</h2><p class="eng">{en}</p></div>{status}</div>'
                     f'<details><summary>查看生成提示词</summary><p>{prompt}</p></details></article>')
    css = """
:root{--bg:#080d12;--surface:#131b22;--line:#34414b;--ink:#f0eee6;--muted:#9baeb7;--accent:#9bc7d2;--gold:#d3b88b}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--bg);color:var(--ink);font-family:Inter,"Noto Sans SC","Microsoft YaHei",Arial,sans-serif}a{color:inherit}
main{max-width:1640px;margin:auto;padding:32px clamp(20px,4.6vw,82px) 90px}
.topline{display:flex;justify-content:space-between;gap:16px;border-bottom:1px solid var(--line);padding:3px 0 20px;color:var(--muted);font-size:12px;letter-spacing:.16em}
.mast{display:grid;grid-template-columns:minmax(0,1fr) minmax(210px,340px);gap:35px;align-items:end;padding:clamp(45px,7vw,105px) 0 70px}
.series{color:var(--accent);font-size:12px;letter-spacing:.3em}h1{font-family:Georgia,"Noto Serif SC",serif;font-weight:400;font-size:clamp(68px,10vw,165px);line-height:.87;letter-spacing:-.06em;margin:29px 0 35px}
h1 em{font-weight:400;color:var(--accent)}.mast-left>p{max-width:550px;line-height:1.85;color:var(--muted);font-size:15px}
.mast-right{text-align:right;border-left:1px solid var(--line);padding:0 0 3px 26px}.big-count{display:block;font:400 clamp(88px,12vw,175px)/.95 Georgia,serif;letter-spacing:-.09em;color:var(--gold)}
.big-count i{font:normal 22px Inter,Arial,sans-serif;letter-spacing:0;color:var(--muted)}.counter-note,.submeta{display:block;font-size:12px;color:var(--muted);letter-spacing:.12em;margin-top:20px}.submeta{margin-top:45px}
.section-name{display:flex;align-items:center;justify-content:space-between;padding:18px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line);margin-bottom:34px;color:var(--muted);font-size:12px;letter-spacing:.19em}
.gallery{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));column-gap:26px;row-gap:55px}.frame{min-width:0}
.frame:first-child{grid-column:span 2}.frame-no{color:var(--muted);letter-spacing:.13em;font-size:11px;margin-bottom:13px}
.visual{display:block;position:relative;aspect-ratio:16/9;overflow:hidden;background:#1b2832;text-decoration:none}
.visual img{width:100%;height:100%;object-fit:cover;display:block;transition:transform .45s cubic-bezier(.2,.75,.25,1)}.visual:hover img{transform:scale(1.025)}
.open{position:absolute;bottom:17px;right:17px;background:rgba(8,13,18,.83);font-size:12px;padding:10px 15px;opacity:0;transition:opacity .2s}
.visual:hover .open,.visual:focus-visible .open{opacity:1}.placeholder{display:grid;place-items:center;color:#829aa5;font:12px Georgia,serif;letter-spacing:.24em}
.frame-details{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-top:17px}
h2{font:400 25px/1.25 Georgia,"Noto Serif SC",serif;margin:0}.eng{color:var(--muted);font-size:12px;margin:7px 0 0}.state{flex:none;font-size:11px;color:var(--muted);border:1px solid var(--line);padding:6px 9px}.state.done{color:var(--accent)}
details{border-top:1px solid #22313a;margin-top:23px;padding-top:14px}summary{cursor:pointer;color:var(--muted);font-size:12px}details p{font-size:12px;line-height:1.85;color:#b9c3ca;max-width:80ch}
footer{border-top:1px solid var(--line);margin-top:72px;padding-top:20px;display:flex;justify-content:space-between;gap:18px;color:var(--muted);font-size:12px}
:focus-visible{outline:2px solid var(--accent);outline-offset:4px}
@media(max-width:750px){.mast{grid-template-columns:1fr;gap:20px;padding-top:55px}.mast-right{text-align:left;border:0;border-top:1px solid var(--line);padding:20px 0}.big-count{font-size:80px}.submeta{margin-top:18px}.gallery{grid-template-columns:1fr;row-gap:40px}.frame:first-child{grid-column:auto}.topline{font-size:10px;letter-spacing:.04em}h1{font-size:clamp(65px,15vw,110px)}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}.visual img{transition:none}}
"""
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta name="color-scheme" content="dark"><title>Cinematic Atlas — 12 frames</title>'
            f'<style>{css}</style></head><body><main>'
            '<div class="topline"><span>MINIMAX H3 IMAGE / MODAL</span><span>HUGGING FACE · ORIGINAL IMAGE ARCHIVE</span></div>'
            +headings+'<div class="section-name"><span>THE CONTACT SHEET</span><span>12 FRAMES</span></div>'
            +'<section class="gallery">'+''.join(cards)+'</section>'
            '<footer><span>由实际归档记录生成 · 无占位图片冒充成品</span><span>点击任意画面打开原图 · 优先本地文件</span></footer></main></body></html>')


def build(root=ROOT, task_name=TASK_NAME):
    root=Path(root)
    titles=read_json(root/"tasks"/f"{task_name}.titles.json")
    direct_records=root/"outputs"/f"{task_name}-direct-results.json"
    if direct_records.exists():
        # The direct Modal GPU path archives outputs to HF without using the
        # older CPU scheduler; these records are validated cloud receipts.
        task=read_json(root/"tasks"/f"{task_name}.json")
        prompts={item["id"]:item["prompt"] for item in task["items"]}
        archived=read_json(direct_records)
        if len(titles)!=12 or len(prompts)!=12 or set(archived)-set(prompts):
            raise ValueError("Invalid cinematic Task or archive receipt IDs")
        items=[]
        for title in titles:
            shot_id=title["id"]
            record=archived.get(shot_id)
            url=record.get("url") if record else None
            if url is not None:
                parsed=urlparse(url)
                if (parsed.scheme!="https" or parsed.hostname!="huggingface.co"
                        or not parsed.path.startswith("/buckets/")):
                    raise ValueError("Invalid archived HF Bucket URL")
            items.append({"id":shot_id,"title":title["title"],"en":title["en"],
                          "prompt":prompts[shot_id],
                          "status":"completed" if url else "pending",
                          "url":url,"bytes":record.get("bytes") if record else None})
    else:
        directory=state.resolve_task(root, "outputs/"+task_name)
        items=collect(directory,titles)
    local_folder=root/"outputs"/task_name
    prefer_local_images(items,local_folder,relative_prefix=quote(task_name)+"/")
    html=render_gallery(items)
    dest=root/"outputs"/f"{task_name}-gallery.html"
    atomic_write(dest,html.encode("utf-8"))
    if local_folder.is_dir() and any(item.get("local_src") for item in items):
        local_items=[{**item,"local_src":item["local_src"].split("/",1)[1]} if item.get("local_src") else item for item in items]
        atomic_write(local_folder/"index.html",render_gallery(local_items).encode("utf-8"))
    return dest,items


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strict",action="store_true",help="Require 12 verified HF image links")
    ap.add_argument("--task", default=TASK_NAME, help="Registered Task name to render")
    args=ap.parse_args()
    output,items=build(task_name=args.task)
    completed=sum(bool(x["url"]) for x in items)
    print(json.dumps({"html":str(output),"hf_images":completed,"expected":len(items)},ensure_ascii=False))
    if args.strict and completed!=12:
        raise SystemExit(1)


if __name__=="__main__":
    main()
