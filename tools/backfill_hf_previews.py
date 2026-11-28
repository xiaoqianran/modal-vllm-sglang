"""Backfill verified remote previews without downloading original files to PC2.

Run from repository root: python -m tools.backfill_hf_previews
"""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from runtime.delivery import v3
from runtime.archive.preview import preview_url, verified_preview
from runtime.utils.files import atomic_json, file_lock, read_json


def receipts(root):
    rows = {}
    def add(row):
        if (isinstance(row, dict) and row.get("status") == "archived"
            and row.get("verification") == "sha256" and row.get("kind") == "image"
            and row.get("url") and row.get("stored_sha256")):
            rows.setdefault(row["url"], row)
    # Latest tasks first, especially those previously missing previews.
    for path in sorted((root / ".runtime/tasks").glob("*/*/state.json"), reverse=True):
        for job in read_json(path)["jobs"].values():
            add(job.get("cloud_archive"))
    history = root / "outputs/.runtime/history-hf-migration/receipts.json"
    if history.exists():
        for row in read_json(history).values():
            add(row)
    for path in sorted((root / "outputs").glob("*-direct-results.json")):
        for row in read_json(path).values():
            if isinstance(row, dict):
                add(row.get("archive"))
    return rows


def main():
    import modal
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    root = Path.cwd()
    index_path = root / "outputs/.runtime/preview-index.json"
    version = v3.build_id()
    rows = receipts(root)
    with file_lock(index_path.parent, filename=".preview-backfill.lock"):
        index = read_json(index_path) if index_path.exists() else {}
        pending = defaultdict(list)
        for url, row in rows.items():
            prior = index.get(url, {})
            if verified_preview(row, index) or (prior.get("status") == "original" and prior.get("source_sha256") == row["stored_sha256"]):
                continue
            pending[row["bucket"]].append(row)
        selected = [row for group in pending.values() for row in group]
        if args.limit > 0:
            selected = selected[:args.limit]
        by_bucket = defaultdict(list)
        for row in selected:
            by_bucket[row["bucket"]].append(row)
        batches = [(bucket, group[i:i+4]) for bucket, group in by_bucket.items() for i in range(0,len(group),4)]
        print("BACKFILL", len(rows), "unique originals;", len(selected), "pending", flush=True)
        if not batches:
            print("FINISHED", {}, flush=True)
            return 0
        fn = modal.Function.from_name(v3.app_name(), "archive_batch")
        counts = Counter()
        def run(batch):
            bucket, group = batch
            return group, fn.remote([], v3.PROTOCOL, backfill={"kind": "image", "bucket": bucket, "receipts": group, "build": version})
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(run, batch): batch for batch in batches}
            for future in as_completed(futures):
                try:
                    group, response = future.result()
                except Exception as exc:
                    counts["error"] += len(futures[future][1])
                    print("BATCH_ERROR", type(exc).__name__, "retryable", len(futures[future][1]), flush=True)
                    continue
                if response.get("archive_version") != version:
                    raise RuntimeError("Archive version mismatch")
                results = response["results"]
                for row in group:
                    url = row["url"]
                    result = results.get(url,{})
                    status = result.get("status")
                    if status == "preview" and verified_preview(row,{url:result}):
                        index[url] = result
                        counts["preview"] += 1
                    elif status == "original" and result.get("source_sha256") == row["stored_sha256"]:
                        index[url] = result
                        counts["original"] += 1
                    else:
                        counts["error"] += 1
                atomic_json(index_path,index)
                print("PROGRESS", dict(counts), flush=True)
        print("FINISHED", dict(counts), flush=True)
        return int(bool(counts["error"]))

if __name__ == "__main__":
    raise SystemExit(main())
