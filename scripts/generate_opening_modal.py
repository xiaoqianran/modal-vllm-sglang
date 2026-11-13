"""Resumable opening-shot entrypoint; uses the same single-GPU MOSS batch runner.

All three existing images are preserved. If any is missing, only that shot runs.
"""
from pathlib import Path
from generate_again_love_v3 import run, ROOT

if __name__ == "__main__":
    run("002-again-love", [ROOT / "tasks" / "002-again-love.json"])
