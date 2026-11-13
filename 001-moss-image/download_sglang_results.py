"""Download one explicitly selected SGLang benchmark; never select latest implicitly."""
import argparse
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.files import read_json, download_volume_file
from shared.outputs import run_dir


def download(run_id, volume):
    if not re.fullmatch(r"\d{8}T\d{6}Z", run_id):
        raise ValueError("Invalid SGLang run ID")
    remote = f"/008-sglang-benchmark/{run_id}"
    local = run_dir("moss-sglang", run_id, benchmark=True)
    for entry in volume.listdir(remote):
        name = entry.path.rsplit("/", 1)[-1]
        if name in {"report.json", "server.log"}:
            destination = local / name
        elif Path(name).suffix.lower() in {".png", ".jpg", ".jpeg"} and "\\" not in name:
            destination = local / "samples" / name
        else:
            continue
        download_volume_file(volume, f"{remote}/{name}", destination)
    print("OUTPUT", local)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--run-id")
    source.add_argument("--report", type=Path)
    args = parser.parse_args()
    run_id = args.run_id
    if args.report:
        remote = read_json(args.report)["volume_dir"]
        if not remote.startswith("/mnt/modal/008-sglang-benchmark/"):
            raise ValueError("Invalid benchmark report volume_dir")
        run_id = remote.rsplit("/", 1)[-1]
    import modal
    download(run_id, modal.Volume.from_name("modal-forge"))


if __name__ == "__main__":
    main()
