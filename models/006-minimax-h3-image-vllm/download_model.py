"""Model-local facade over the benchmark downloader and cache maintenance CLI.

No cloud access without an explicit --execute flag.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.download_models import main as download_main


def main(argv=None):
    download_main([str(Path(__file__).with_name("model.json")), *(argv or [])])


if __name__ == "__main__":
    main(sys.argv[1:])
