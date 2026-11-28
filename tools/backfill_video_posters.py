"""Build remote, content-addressed posters for archived videos on Modal CPU."""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from runtime.delivery import v3
from runtime.archive.huggingface import public_url
from runtime.archive.video_poster import POLICY
from runtime.utils.files import atomic_json, file_lock, read_json


def receipts(root):
    rows = {}
    def add(row):
        if (isinstance(row,dict) and row.get("status")=="archived"
            and row.get("verification")=="sha256" and row.get("kind")=="video"
            and row.get("url") and row.get("stored_sha256")):
            rows.setdefault(row["url"],row)
    for path in sorted((root/".runtime/tasks").glob("*/*/state.json"),reverse=True):
        for job in read_json(path).get("jobs",{}).values():
            add(job.get("cloud_archive"))
    history=root/"outputs/.runtime/history-hf-migration/receipts.json"
    if history.exists():
        for row in read_json(history).values(): add(row)
    for path in sorted((root/"outputs").glob("*-direct-results.json")):
        for row in read_json(path).values():
            if isinstance(row,dict): add(row.get("archive"))
    return rows


def valid_poster(row, receipt):
    try:
        digest=row["sha256"]; path=row["path"]
        return (row["source_sha256"]==receipt["stored_sha256"]
            and row["source_bytes"]==receipt["stored_bytes"]
            and row["kind"]=="video" and row["policy"]==POLICY
            and row["verification"]=="sha256" and len(digest)==64
            and path==f"thumbnails/sha256/{digest[:2]}/{digest}.webp"
            and row["url"]==public_url(receipt["bucket"],path)
            and type(row["bytes"]) is int and 0<row["bytes"]<=512*1024)
    except (KeyError,TypeError,ValueError):
        return False


def main():
    import modal
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit",type=int,default=0)
    args=parser.parse_args()
    root=Path.cwd()
    index_path=root/"outputs/.runtime/gallery-derivatives.json"
    version = v3.build_id()
    rows=receipts(root)
    with file_lock(index_path.parent,filename=".video-poster-backfill.lock"):
        index=read_json(index_path) if index_path.exists() else {}
        pending=[row for row in rows.values()
                 if not valid_poster(index.get(row["url"],{}),row)]
        if args.limit>0: pending=pending[:args.limit]
        print("VIDEO_POSTERS",len(rows),"unique videos;",len(pending),"pending",flush=True)
        if not pending: return 0
        grouped=defaultdict(list)
        for row in pending: grouped[row["bucket"]].append(row)
        batches=[(bucket,items[i:i+4]) for bucket,items in grouped.items() for i in range(0,len(items),4)]
        fn=modal.Function.from_name(v3.app_name(), "archive_batch")
        counts=Counter()
        def run(batch):
            bucket,items=batch
            return items,fn.remote([], v3.PROTOCOL, backfill={"kind": "video", "bucket": bucket, "receipts": items, "build": version})
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures={pool.submit(run,b):b for b in batches}
            for future in as_completed(futures):
                try:
                    items,response=future.result()
                    if response.get("archive_version")!=version:
                        raise RuntimeError("Archive deployment version mismatch")
                    for receipt in items:
                        url=receipt["url"]; result=response["results"].get(url,{})
                        poster=result.get("poster",{})
                        if result.get("status")=="poster" and valid_poster(poster,receipt):
                            index[url]={"source_sha256":poster["source_sha256"],
                                "source_bytes":receipt["stored_bytes"],"kind":"video",
                                "policy":POLICY,"verification":"sha256","sha256":poster["sha256"],
                                "bytes":poster["bytes"],"path":poster["path"],
                                "dimensions":poster["dimensions"],"url":poster["url"]}
                            counts["poster"]+=1
                            if result.get("reused"): counts["reused"]+=1
                        else:
                            counts["error"]+=1
                            print("POSTER_ERROR",url,result.get("error","invalid result"),flush=True)
                except Exception as exc:
                    counts["error"]+=len(futures[future][1])
                    print("BATCH_ERROR",type(exc).__name__,flush=True)
                atomic_json(index_path,index)
                print("PROGRESS",dict(counts),flush=True)
        print("FINISHED",dict(counts),flush=True)
        return int(bool(counts["error"]))

if __name__=="__main__":
    raise SystemExit(main())
