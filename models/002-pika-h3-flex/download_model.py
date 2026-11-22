"""Pika CLI entrypoint for the lightweight benchmark download pipeline.

Run: uv run python models/002-pika-h3-flex/download_model.py [--execute]
Without --execute, no paid Modal resources are started.
"""
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from tools.download_models import main as download_main


def main(argv=None):
    args = [] if argv is None else list(argv)
    download_main([str(Path(__file__).with_name("model.json")), *args])


if __name__ == "__main__":
    main(sys.argv[1:])
