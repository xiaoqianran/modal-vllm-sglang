"""Inventory and copy legacy local results; never delete originals or call GPU APIs."""
import argparse
import copy
import json
import os
from pathlib import Path
import re
import shutil
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.files import atomic_json, read_json, sha256_file
from shared.outputs import REPO, component, record_artifact
from orchestration.state import lock, local_path


def inventory(root=REPO):
    root = Path(root).resolve()
    entries, groups = [], {}
    for model, alias in (("001-moss-image", "moss"), ("002-pika-h3-flex", "pika")):
        source_root = root / model / "results"
        for source in sorted(source_root.rglob("*")):
            if not source.is_file():
                continue
            if source.is_symlink() or not source.resolve().is_relative_to(source_root.resolve()):
                raise ValueError(f"Refuse symlink/outside source: {source}")
            parts = source.relative_to(source_root).parts
            run, item, kind = None, None, None
            destination = Path("outputs/_legacy") / alias / Path(*parts)
            if alias == "moss" and len(parts) == 4 and parts[0] == "storyboard":
                run = Path("outputs") / component(parts[1]) / ("legacy-" + component(parts[2]))
                kind = "image"
            elif alias == "pika" and len(parts) == 4 and parts[0] in {"t2va", "fl2va"}:
                run = Path("outputs") / component(parts[1]) / ("legacy-" + component(parts[2]))
                kind = "video"
            elif alias == "moss" and len(parts) == 3 and parts[0] == "sglang":
                base = Path("benchmarks/moss-sglang") / ("legacy-" + component(parts[1]))
                destination = base / ("samples" if source.suffix.lower() in {".png", ".jpg", ".jpeg"} else "") / source.name
            elif alias == "moss" and len(parts) == 1 and re.fullmatch(r"vllm_.+_\d{8}T\d{6}Z\.json", source.name):
                destination = Path("benchmarks/moss-vllm") / ("legacy-" + source.stem) / "report.json"
            if run:
                if source.name == "report.json":
                    destination = run / "source-report.json"
                    groups[run.as_posix()] = {"source_report": source.relative_to(root).as_posix(), "kind": kind}
                elif source.suffix.lower() in {".png", ".jpg", ".jpeg", ".mp4"}:
                    item = component(source.stem)
                    destination = run / "items" / item / kind / (kind + source.suffix.lower())
                elif source.suffix == ".json" and not source.name.endswith(".error.json"):
                    item = component(source.stem)
                    destination = run / "items" / item / kind / "source-metadata.json"
                else:
                    destination = run / "legacy" / source.name
            entries.append({"source": source.relative_to(root).as_posix(), "destination": destination.as_posix(),
                            "size": source.stat().st_size, "sha256": sha256_file(source),
                            "run": run.as_posix() if run else None,
                            "kind": kind if item and source.suffix.lower() != ".json" else None, "item": item})
    destinations = [e["destination"].lower() for e in entries]
    if len(destinations) != len(set(destinations)):
        raise ValueError("Conflicting legacy destinations; resolve inventory before copying")
    return {"version": 1, "policy": "copy-and-verify-preserve-originals", "files": entries, "groups": groups}


def copy_verified(root, entry):
    source, destination = (local_path(root, entry[k]) for k in ("source", "destination"))
    if source.stat().st_size != entry["size"] or sha256_file(source) != entry["sha256"]:
        raise ValueError(f"Source changed: {entry['source']}")
    if destination.exists():
        if sha256_file(destination) != entry["sha256"]:
            raise ValueError(f"Destination differs; refusing overwrite: {entry['destination']}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".part")
    try:
        shutil.copyfile(source, temp)
        if sha256_file(temp) != entry["sha256"] or sha256_file(source) != entry["sha256"]:
            raise ValueError("Copy verification failed or source changed")
        os.replace(temp, destination)
    finally:
        temp.unlink(missing_ok=True)


def apply(plan, root=REPO):
    root = Path(root).resolve()
    with lock(root / "outputs"):
        journal = root / "outputs/_migration/legacy-v1.json"
        if journal.exists():
            old = read_json(journal)
            if old["inventory"] != plan:
                raise ValueError("Legacy inventory changed; preserve/review existing journal before another import")
        atomic_json(journal, {"status": "copying", "inventory": plan})
        for entry in plan["files"]:
            copy_verified(root, entry)
        for run, group in plan["groups"].items():
            directory = local_path(root, run)
            report = copy.deepcopy(read_json(directory / "source-report.json"))
            report.update(entrypoint="legacy-import", local_layout="task-run-items", resume_supported=False,
                          source_report=group["source_report"])
            outputs = {}
            for entry in plan["files"]:
                if entry["run"] == run and entry["kind"]:
                    path = local_path(root, entry["destination"])
                    artifact = record_artifact(directory, path, entry["kind"], source=entry["source"])
                    outputs[entry["item"]] = artifact
            report["local_outputs"] = outputs
            for result in report.get("shots", report.get("results", report.get("items", []))):
                if result.get("id") in outputs:
                    result["local_path"] = outputs[result["id"]]["path"]
            report_path = directory / "report.json"
            if report_path.exists() and read_json(report_path) != report:
                raise ValueError(f"Existing report differs: {report_path}")
            atomic_json(report_path, report)
        for entry in plan["files"]:
            if sha256_file(local_path(root, entry["destination"])) != entry["sha256"]:
                raise ValueError("Final integrity verification failed")
        atomic_json(journal, {"status": "verified", "inventory": plan,
                              "files_verified": len(plan["files"]), "originals_preserved": True})
    return journal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Copy files and verify SHA256; originals remain")
    args = parser.parse_args()
    plan = inventory()
    summary = {"files": len(plan["files"]), "bytes": sum(e["size"] for e in plan["files"]),
               "destinations": sorted({str(Path(e["destination"]).parent) for e in plan["files"]})}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.apply:
        print("VERIFIED_MANIFEST", apply(plan))


if __name__ == "__main__":
    main()
