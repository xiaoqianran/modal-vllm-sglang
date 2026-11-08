"""Download SGLang images from the persistent Modal Volume to PC2."""
import os
from pathlib import Path
import re

import modal

root = Path(__file__).parent
volume = modal.Volume.from_name("modal-forge")
base_dir = "/008-sglang-benchmark"
run_id = os.environ.get("SGLANG_RUN_ID")
if not run_id:
    candidates = sorted(
        entry.path.rsplit("/", 1)[-1]
        for entry in volume.listdir(base_dir)
        if re.fullmatch(r"\d{8}T\d{6}Z", entry.path.rsplit("/", 1)[-1])
    )
    if not candidates:
        raise FileNotFoundError("No SGLang benchmark results on Modal Volume")
    run_id = candidates[-1]
if not re.fullmatch(r"\d{8}T\d{6}Z", run_id):
    raise ValueError("Invalid SGLANG_RUN_ID")

remote_dir = f"{base_dir}/{run_id}"
local_dir = root / "results" / "sglang" / run_id
local_dir.mkdir(parents=True, exist_ok=True)
for entry in volume.listdir(remote_dir):
    name = entry.path.rsplit("/", 1)[-1]
    if not name.lower().endswith((".png", ".jpg", ".jpeg", ".json")):
        continue
    destination = local_dir / name
    with destination.open("wb") as output:
        for chunk in volume.read_file(entry.path):
            output.write(chunk)
    print(f"SAVED {name}: {destination.stat().st_size} bytes", flush=True)
print("OUTPUT", local_dir)
