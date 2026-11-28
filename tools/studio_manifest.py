"""Content-addressed Studio recovery snapshots. No inference or media upload.

The public snapshot deliberately excludes prompts, local paths, and runner state.
It restores a read-only gallery, not a runnable task or a full metadata backup.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from urllib.request import Request, urlopen

from orchestration import studio
from runtime.archive.config import DEFAULT_ASSET_BUCKET
from runtime.archive.huggingface import HuggingFaceBucket, public_url
from runtime.archive.preview import verified_preview
from runtime.utils.files import atomic_write, read_json

MAX_BYTES = 8 * 1024 * 1024
HEX = re.compile(r"[0-9a-f]{64}")
SCHEMA = "studio-gallery-v1"


def encode(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def address(payload):
    digest = hashlib.sha256(payload).hexdigest()
    return f"manifests/studio/v1/{digest[:2]}/{digest}.json"


def receipt_is_valid(row, bucket):
    try:
        suffix = PurePosixPath(row["path"]).suffix.lower()
        return (row["status"] == "archived" and row["verification"] == "sha256"
                and row["bucket"] == bucket and row["url"] == public_url(bucket, row["path"])
                and bool(HEX.fullmatch(row["stored_sha256"]))
                and PurePosixPath(row["path"]).stem == row["stored_sha256"]
                and type(row["stored_bytes"]) is int and row["stored_bytes"] > 0
                and ((row["kind"] == "image" and suffix in {".png", ".jpg", ".jpeg", ".webp"})
                     or (row["kind"] == "video" and suffix == ".mp4")))
    except (TypeError, KeyError, ValueError, AttributeError):
        return False


def receipt_rows(root):
    output = root / "outputs"
    yield from (studio.read_json(output / studio.RECEIPTS, {}) or {}).values()
    for path in sorted((root / ".runtime/tasks").glob("*/*/state.json")):
        for job in read_json(path).get("jobs", {}).values():
            if isinstance(job.get("cloud_archive"), dict):
                yield job["cloud_archive"]
    for path in sorted(output.glob("*-direct-results.json")):
        for record in read_json(path).values():
            if isinstance(record, dict) and isinstance(record.get("archive"), dict):
                yield record["archive"]


def build(root, bucket=DEFAULT_ASSET_BUCKET):
    root = Path(root).resolve()
    data = studio.collect(root / "outputs", thumbs=False)
    rows = {}
    for row in receipt_rows(root):
        if receipt_is_valid(row, bucket):
            rows.setdefault(row["url"], []).append(row)
    index = studio.read_json(root / "outputs/.runtime/preview-index.json", {}) or {}
    assets, missing = [], []
    for asset in data["assets"]:
        matches = [r for r in rows.get(asset["url"], []) if r["stored_bytes"] == asset["bytes"] and r["kind"] == asset["kind"]]
        if asset["storage"] != "cloud" or not matches:
            missing.append(asset["id"])
            continue
        row = matches[0]
        thumb = next((v for r in matches if (v := verified_preview(r, index))), "")
        entry = {k: asset[k] for k in ("id", "project", "name", "kind", "dimensions") if k in asset}
        entry.update(path=row["path"], sha256=row["stored_sha256"], bytes=row["stored_bytes"])
        if thumb:
            # The verified mapping supplies the digest and size, not just a URL.
            candidates = [r.get("preview") for r in matches] + [index.get(row["url"], {}).get("preview")]
            for candidate in candidates:
                if isinstance(candidate, dict) and candidate.get("url") == thumb:
                    entry["preview"] = {k: candidate[k] for k in ("path", "sha256", "bytes")}
                    break
        assets.append(entry)
    if missing:
        raise ValueError(f"Refusing incomplete snapshot: {len(missing)} missing verified receipts: {missing[:10]}")
    # Task IDs and media names are required for grouping; omit prompts, titles,
    # machine paths, telemetry, and executable task records from public metadata.
    result = {"schema": SCHEMA, "bucket": bucket, "assets": sorted(assets, key=lambda a: a["id"]),
              "projects": sorted({a["project"] for a in assets})}
    validate(result)
    return result


def validate(data):
    if not isinstance(data, dict) or set(data) != {"schema", "bucket", "assets", "projects"} or data["schema"] != SCHEMA:
        raise ValueError("Invalid manifest schema")
    bucket = data["bucket"]
    public_url(bucket, "check")
    ids, projects = set(), set()
    if not isinstance(data["assets"], list) or not data["assets"]:
        raise ValueError("Empty or invalid asset inventory")
    for a in data["assets"]:
        if not isinstance(a, dict) or not set(a) <= {"id", "project", "name", "kind", "dimensions", "path", "sha256", "bytes", "preview"}:
            raise ValueError("Invalid asset fields")
        if any(not isinstance(a[k], str) or not a[k] or len(a[k]) > 512 for k in ("id", "project", "name")) or a["id"] in ids:
            raise ValueError("Invalid or duplicate asset identity")
        ids.add(a["id"]); projects.add(a["project"])
        if not receipt_is_valid({"status":"archived", "verification":"sha256", "bucket":bucket, "url":public_url(bucket,a["path"]), "path":a["path"], "kind":a["kind"], "stored_sha256":a["sha256"], "stored_bytes":a["bytes"]},bucket):
            raise ValueError("Invalid asset address or digest")
        dims = a.get("dimensions")
        if dims is not None and (not isinstance(dims, (list, tuple)) or len(dims)!=2 or any(type(x) is not int or x<=0 for x in dims)):
            raise ValueError("Invalid dimensions")
        if "preview" in a:
            p=a["preview"]; d=p["sha256"]
            if (set(p)!={"path","sha256","bytes"} or not HEX.fullmatch(d) or p["path"]!=f"previews/sha256/{d[:2]}/{d}.webp" or type(p["bytes"]) is not int or p["bytes"]<=0):
                raise ValueError("Invalid preview")
    if data["projects"] != sorted(projects):
        raise ValueError("Project inventory mismatch")
    return data


def decode(payload, expected_path=None):
    if len(payload)>MAX_BYTES or (expected_path and address(payload)!=expected_path):
        raise ValueError("Manifest size or SHA-256 mismatch")
    return validate(json.loads(payload))


def fetch(bucket, remote):
    if not re.fullmatch(r"manifests/studio/v1/[0-9a-f]{2}/[0-9a-f]{64}\.json",remote):
        raise ValueError("Expected a content-addressed Studio manifest path")
    with urlopen(Request(public_url(bucket,remote),headers={"Accept-Encoding":"identity"}),timeout=30) as response:
        payload=response.read(MAX_BYTES+1)
    data=decode(payload,remote)
    if data["bucket"]!=bucket:
        raise ValueError("Manifest bucket mismatch")
    return payload


def discover(bucket):
    """List immutable recovery points, without relying on a local pointer."""
    from huggingface_hub import list_bucket_tree
    prefix = "manifests/studio/v1"
    rows = []
    for item in list_bucket_tree(bucket, prefix=prefix, recursive=True, token=False):
        path = getattr(item, "path", None)
        if isinstance(path, str) and re.fullmatch(r"manifests/studio/v1/[0-9a-f]{2}/[0-9a-f]{64}\.json", path):
            rows.append(path)
    return sorted(set(rows))


def restore(payload, destination):
    data=decode(payload)
    root=Path(destination)
    if root.exists() and any(root.iterdir()):
        raise FileExistsError("Restore destination must be empty")
    assets=[]
    for row in data["assets"]:
        url=public_url(data["bucket"],row["path"])
        item={k:row[k] for k in ("id","project","name","kind","bytes")}
        item.update(url=url, storage="cloud", digest=row["sha256"], title=row["name"], dimensions=row.get("dimensions"),
                    thumb=public_url(data["bucket"],row["preview"]["path"]) if "preview" in row else url if row["kind"]=="image" else "")
        assets.append(item)
    initial={"assets":assets,"tasks":[],"bucket_url":"https://huggingface.co/buckets/"+data["bucket"],"view":"gallery","selected":None,"catalog_url":None}
    template=studio.TEMPLATE.read_text(encoding="utf-8")
    if template.count("__STUDIO_DATA__")!=1:
        raise ValueError("Invalid Studio template")
    embedded=encode(initial).decode().replace("<","\\u003c").replace(">","\\u003e").replace("&","\\u0026")
    atomic_write(root/"index.html",template.replace("__STUDIO_DATA__",embedded).encode())
    atomic_write(root/"manifest.json",payload)
    return len(assets)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest="command",required=True)
    create=sub.add_parser("export"); create.add_argument("--root",type=Path,default=Path.cwd()); create.add_argument("--output",type=Path,required=True); create.add_argument("--publish",action="store_true",help="Publish sanitized metadata (project IDs and filenames) to the public HF bucket")
    recover=sub.add_parser("restore"); source=recover.add_mutually_exclusive_group(required=True); source.add_argument("--manifest",type=Path); source.add_argument("--remote"); recover.add_argument("--output",type=Path,required=True); recover.add_argument("--bucket",default=DEFAULT_ASSET_BUCKET)
    listing=sub.add_parser("list"); listing.add_argument("--bucket",default=DEFAULT_ASSET_BUCKET)
    args=parser.parse_args()
    if args.command=="export":
        data=build(args.root); payload=encode(data); remote=address(payload)
        decode(payload,remote); atomic_write(args.output,payload)
        print("SNAPSHOT",len(data["assets"]),"assets",len(payload),"bytes",remote)
        if args.publish:
            provider=HuggingFaceBucket(data["bucket"])
            provider.publish(args.output,remote,"sha256",expected_sha256=hashlib.sha256(payload).hexdigest())
            if fetch(data["bucket"],remote)!=payload: raise RuntimeError("Remote manifest mismatch")
            print("VERIFIED",public_url(data["bucket"],remote))
    elif args.command=="list":
        for path in discover(args.bucket): print(path)
    else:
        payload=args.manifest.read_bytes() if args.manifest else fetch(args.bucket,args.remote)
        print("RESTORED",restore(payload,args.output),"assets",args.output)


if __name__=="__main__":
    main()
