"""Run MiniMax H3 Image on Modal, upload images from the cloud to HF, render HTML.

Weights and generated image bytes never travel through the local PC.
For failed/uncertain submissions, inspect Volume metadata before retrying.
"""
import argparse
import json
from pathlib import Path
import re
from urllib.parse import urlparse

from runtime.config.model_spec import validate
from runtime.config.storage import VOLUME_VERSION, runtime_volume_name
from runtime.tasks.job_client import remote_cache_fingerprint
from runtime.tasks.task_identity import intent_digest
from runtime.tasks.task_spec import prepare, result_prefix
from runtime.delivery.config import normalize_delivery
from runtime.archive.config import ASSET_APP_NAME
from runtime.utils.files import atomic_json, read_json
from tools.build_cinematic_gallery import render_gallery

ROOT = Path(__file__).resolve().parents[1]
TASK_NAME = "022-minimax-cinematic-atlas-12"
PROJECT = "cinematic-atlas-022"
SCENE = "001-main"


def verified_bucket_url(url):
    if not isinstance(url, str):
        raise ValueError("Missing HF image URL")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "huggingface.co" or not parsed.path.startswith("/buckets/"):
        raise ValueError("Invalid HF Bucket image URL")
    return url


def records_for_gallery(titles, items, records):
    lookup = {item["id"]: item for item in items}
    return [
        {
            "id": title["id"], "title": title["title"], "en": title["en"],
            "prompt": lookup[title["id"]]["prompt"],
            "status": "completed" if title["id"] in records else "pending",
            "url": verified_bucket_url(records[title["id"]]["url"]) if title["id"] in records else None,
            "bytes": records.get(title["id"], {}).get("bytes"),
        }
        for title in titles
    ]


def run(*, limit=12):
    import modal
    if not 1 <= limit <= 12:
        raise ValueError("limit must be 1..12")
    spec = validate(read_json(ROOT / "models/005-minimax-h3-image/model.json"))
    task = read_json(ROOT / "tasks" / f"{TASK_NAME}.json")
    titles = read_json(ROOT / "tasks" / f"{TASK_NAME}.titles.json")
    if len(task["items"]) != 12 or len(titles) != 12:
        raise ValueError("Task must contain exactly 12 scenes")
    delivery = normalize_delivery(task["delivery"])
    path = ROOT / "outputs" / f"{TASK_NAME}-direct-results.json"
    gallery = ROOT / "outputs" / f"{TASK_NAME}-gallery.html"
    records = read_json(path) if path.exists() else {}
    if set(records) - {item["id"] for item in task["items"]}:
        raise ValueError("Unrecognized archived image ID")
    volume = modal.Volume.from_name(runtime_volume_name(), version=VOLUME_VERSION)
    fingerprint = remote_cache_fingerprint(spec, volume)
    model = modal.Cls.from_name(spec["app_name"], spec["modal_class"])()
    archiver = modal.Function.from_name(ASSET_APP_NAME, "archive_asset")
    prefix = result_prefix(spec, PROJECT, SCENE, spec["operation"])
    for item in task["items"][:limit]:
        shot_id = item["id"]
        _, signature = prepare(spec, item)
        if shot_id in records:
            if records[shot_id]["intent_hash"] != signature or records[shot_id]["fingerprint"] != fingerprint:
                raise ValueError(f"Archived image {shot_id} no longer matches immutable prompt/cache")
            print("REUSED_ARCHIVE",shot_id,records[shot_id]["url"],flush=True)
            continue
        print("GENERATE_SUBMIT",shot_id,flush=True)
        result = model.generate_one.remote(item, prefix, fingerprint, intent_digest(spec))
        if result.get("status") != "completed" or result.get("id") != shot_id:
            raise RuntimeError(f"GPU generation did not complete for {shot_id}")
        file_name = result["file"]
        if not re.fullmatch(re.escape(shot_id) + r"\.(?:png|jpg|jpeg|webp)", file_name):
            raise ValueError("Unsafe result filename")
        artifact = {
            "storage": "volume", "path": prefix + "/" + file_name, "type": "image",
            "size": result["bytes"], "sha256": result["sha256"],
        }
        print("GPU_COMPLETED",shot_id,artifact["size"],flush=True)
        archived = archiver.remote(artifact, delivery, PROJECT)
        url = verified_bucket_url(archived["url"])
        if archived.get("status") != "archived" or archived.get("verification") not in ("size","sha256"):
            raise RuntimeError("HF archive not verified")
        records[shot_id] = {
            "url": url, "bytes": result["bytes"], "sha256": result["sha256"],
            "intent_hash": signature, "fingerprint": fingerprint,
            "archive": archived,
        }
        atomic_json(path, records)
        gallery.write_text(render_gallery(records_for_gallery(titles,task["items"],records)),encoding="utf-8")
        print("HF_PUBLISHED",shot_id,url,flush=True)
    gallery.write_text(render_gallery(records_for_gallery(titles,task["items"],records)),encoding="utf-8")
    print("GALLERY",json.dumps({"path":str(gallery),"published":len(records),"expected":12},ensure_ascii=False),flush=True)
    if limit==12 and len(records)!=12:
        raise RuntimeError("Gallery incomplete")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit",type=int,default=12)
    args=parser.parse_args()
    run(limit=args.limit)


if __name__=="__main__":
    main()
