"""Generic entrypoint for any drama's resumable MOSS storyboard batches.

Example:
    uv run --project 001-moss-image python scripts/run_moss_batch.py --project my-drama --task tasks/my-drama-001.json --task tasks/my-drama-002.json
"""
from generate_again_love_v3 import main

if __name__ == "__main__":
    main()
