"""Download an existing storyboard batch without rerunning GPU inference."""
import json
from pathlib import Path

import modal

root = Path(__file__).parent
reports = sorted((root / "results" / "storyboard" / "episode07").glob("*/report.json"))
if not reports:
    raise SystemExit("No completed storyboard report")
report_path = reports[-1]
report = json.loads(report_path.read_text(encoding="utf-8"))
volume = modal.Volume.from_name("modal-forge")
for shot in report["shots"]:
    if shot["status"] != "ok":
        continue
    target = report_path.parent / shot["file"]
    if target.is_file() and target.stat().st_size == shot["bytes"]:
        print("SKIP", shot["id"], flush=True)
        continue
    path = "/" + report["output_prefix"] + "/" + shot["file"]
    with target.open("wb") as f:
        for chunk in volume.read_file(path):
            f.write(chunk)
    print("SAVED", shot["id"], target.stat().st_size, flush=True)
print("LOCAL_DIR", str(report_path.parent))
