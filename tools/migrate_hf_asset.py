"""Dry-run or explicitly copy one legacy HF receipt to global CAS."""
import argparse
import json
from pathlib import Path

from runtime.archive.huggingface import HuggingFaceBucket
from runtime.archive.migration import migrate_receipt, migration_plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--copy", action="store_true", help="Enable non-destructive Xet copy")
    args = parser.parse_args()
    receipt = json.loads(args.receipt.read_text(encoding="utf-8-sig"))
    plan = migration_plan(receipt)
    result = migrate_receipt(HuggingFaceBucket(plan["bucket"]), receipt) if args.copy else plan
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
