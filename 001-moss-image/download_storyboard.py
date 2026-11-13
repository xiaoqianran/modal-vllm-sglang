"""Download one explicitly selected storyboard report, without GPU inference."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.files import read_json, atomic_json, download_volume_file, sha256_file
from shared.outputs import run_dir, step_dir, record_artifact, component


def download(report_path, volume):
    report = read_json(report_path)
    prefix = report["output_prefix"].strip("/")
    parts = prefix.split("/")
    if len(parts) != 3 or parts[0] != "008-moss-storyboard":
        raise ValueError("Expected an 008-moss-storyboard remote prefix")
    for part in parts:
        component(part)
    location = Path(report_path).resolve()
    local = (location.parents[2] if location.parent.name == "moss-batch" and location.parent.parent.name == ".runner"
             else location.parent if report.get("entrypoint") == "moss-modal-batch"
             else run_dir(parts[1]))
    for shot in report["shots"]:
        if shot["status"] not in ("ok", "skipped"):
            continue
        name = shot["file"]
        if Path(name).name != name or "/" in name or "\\" in name or Path(name).suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            raise ValueError("Invalid remote image filename")
        folder = step_dir(local, shot["id"], "image")
        target = local / (shot["id"] + Path(name).suffix)
        manifest = folder / "artifacts.json"
        saved = read_json(manifest).get("artifacts", {}).get("image", {}) if manifest.exists() else {}
        if target.exists() and saved.get("sha256") != sha256_file(target):
            raise FileExistsError(f"Refusing to overwrite existing image: {target}")
        if not (target.is_file() and saved.get("sha256") == sha256_file(target)):
            download_volume_file(volume, f"/{prefix}/{name}", target)
        shot["local_path"] = target.relative_to(local).as_posix()
        record_artifact(local, target, "image", source=f"/{prefix}/{name}")
    report["entrypoint"] = "moss-modal-batch"
    atomic_json(local / ".runner" / "moss-batch" / "report.json", report)
    print("LOCAL_DIR", local)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    import modal
    download(args.report, modal.Volume.from_name("modal-forge"))


if __name__ == "__main__":
    main()
