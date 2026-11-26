"""Read-only HF archive reference inventory. Never deletes assets."""
import argparse
import json
from pathlib import Path

from huggingface_hub import list_bucket_tree, BucketFile

from runtime.archive.config import DEFAULT_ASSET_BUCKET
from runtime.archive.huggingface import HuggingFaceBucket
from runtime.archive.migration import audit_references


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipts", nargs="+", type=Path)
    parser.add_argument("--bucket", default=DEFAULT_ASSET_BUCKET)
    args = parser.parse_args()
    receipts = [json.loads(path.read_text(encoding="utf-8-sig")) for path in args.receipts]
    report = audit_references(receipts, args.bucket)
    provider = HuggingFaceBucket(args.bucket)
    objects = {entry.path: entry.size for entry in list_bucket_tree(
        args.bucket, prefix="assets/sha256", recursive=True, token=provider.token)
        if isinstance(entry, BucketFile)}
    known = report["known_references"]
    report["untracked_assets"] = {path: size for path, size in objects.items() if path not in known}
    report["untracked_does_not_mean_unused"] = True
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
