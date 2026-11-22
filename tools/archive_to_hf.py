"""Manually archive existing outputs; default command is offline dry-run."""
import argparse
import json
from pathlib import Path

from runtime.archive import ArchivePipeline, ArchiveSettings


def main() -> None:
    parser = argparse.ArgumentParser(description="Opt-in HF archive; outputs stay unchanged")
    parser.add_argument("source", type=Path)
    parser.add_argument("--bucket", required=True, help="Public bucket owner/name")
    parser.add_argument("--project", required=True)
    parser.add_argument("--compression", choices=["none", "adaptive_lossless"],
                        default="adaptive_lossless")
    parser.add_argument("--min-saving", type=float, default=0.05)
    parser.add_argument("--verify", choices=["size", "sha256"], default="size")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--upload", action="store_true",
                        help="Explicitly enable network upload (requires HF_TOKEN)")
    args = parser.parse_args()
    settings = ArchiveSettings(args.bucket, args.project, args.compression,
                               args.min_saving, args.verify)
    result = ArchivePipeline(settings).run(args.source, upload=args.upload,
                                          manifest=args.manifest)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
