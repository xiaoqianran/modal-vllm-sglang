"""Migrate visible run-id folders to project/scene with flat media and audited archives.

Dry-run by default. Never re-encodes files or overwrites differing content.
Original run folders are moved under outputs/_migration/_archived_runs after
the new media copies pass SHA256 verification.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from shared.files import sha256_file, atomic_json
from orchestration.state import lock

MEDIA = {".png", ".jpg", ".jpeg", ".webp", ".mp4"}
ARCHIVE = Path("_migration/_archived_runs")


def collect(base):
    base = Path(base).resolve()
    moves = []
    for project in sorted(base.iterdir()):
        if not project.is_dir() or project.name.startswith("_"):
            continue
        for run in sorted(project.iterdir()):
            if not run.is_dir():
                continue
            # Already-migrated scene directories have .runner or the scene naming scheme.
            if run.name.startswith(".") or (
                len(run.name) > 4 and run.name[:3].isdigit() and run.name[3] == "-"
            ):
                continue
            target_project = "002-again-love" if project.name == "002-again-love-opening" else project.name
            scene = "001-hotel-corridor" if project.name == "002-again-love-opening" else "001-main"
            target = base / target_project / scene
            archive = base / ARCHIVE / project.name / run.name
            if archive.exists():
                raise FileExistsError(f"Archive conflict: {archive}")
            media = []
            for p in sorted(run.rglob("*")):
                if not p.is_file():
                    continue
                if p.is_symlink() or not p.resolve().is_relative_to(run.resolve()):
                    raise ValueError(f"Unsafe source: {p}")
                if p.suffix.lower() not in MEDIA:
                    continue
                parts = p.relative_to(run).parts
                if len(parts) >= 4 and parts[0] == "items":
                    stem = parts[1]
                    if stem.lower() in {"con", "nul", "aux", "prn"}:
                        raise ValueError("Reserved item name")
                    destination = target / (stem + p.suffix.lower())
                elif p.parent == run:
                    destination = target / p.name
                else:
                    raise ValueError(f"Unrecognized media placement: {p}")
                media.append((p, destination, sha256_file(p)))
            moves.append({"source": run, "archive": archive, "target": target, "files": media})
    # Preflight all destination conflicts, including across run folders.
    occupied = {}
    for entry in moves:
        for src, dst, digest in entry["files"]:
            key = str(dst).casefold()
            if key in occupied and occupied[key] != digest:
                raise ValueError(f"Two historical runs differ for: {dst}")
            occupied[key] = digest
            if dst.exists() and sha256_file(dst) != digest:
                raise FileExistsError(f"Existing file differs: {dst}")
    return moves


def migrate(base, *, apply=False):
    base = Path(base).resolve()
    with lock(base):
        moves = collect(base)
        inventory = [
            {"from": str(entry["source"].relative_to(base)),
             "to": str(entry["target"].relative_to(base)),
             "files": [{"name": dst.name, "sha256": digest} for _, dst, digest in entry["files"]]}
            for entry in moves
        ]
        if not apply:
            return {"dry_run": True, "runs": len(moves),
                    "media": sum(len(x["files"]) for x in moves), "inventory": inventory}
        for entry in moves:
            for source, dest, digest in entry["files"]:
                dest.parent.mkdir(parents=True, exist_ok=True)
                if not dest.exists():
                    temporary = dest.with_name(dest.name + "." + uuid.uuid4().hex + ".part")
                    try:
                        shutil.copyfile(source, temporary)
                        if sha256_file(temporary) != digest:
                            raise ValueError(f"Hash mismatch: {source}")
                        os.replace(temporary, dest)
                    finally:
                        temporary.unlink(missing_ok=True)
                if sha256_file(dest) != digest:
                    raise ValueError(f"Final checksum mismatch: {dest}")
            entry["archive"].parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(entry["source"]), str(entry["archive"]))
            if not any(entry["source"].parent.iterdir()):
                entry["source"].parent.rmdir()
        journal = base / "_migration" / "scene-layout-v3.json"
        atomic_json(journal, {"schema": 3, "status": "verified", "runs": len(moves),
                              "files": sum(len(x["files"]) for x in moves),
                              "inventory": inventory,
                              "original_runs_archived": True})
        return {"dry_run": False, "runs": len(moves),
                "media": sum(len(x["files"]) for x in moves), "journal": str(journal)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--root", type=Path, default=ROOT / "outputs")
    args = parser.parse_args()
    print(json.dumps(migrate(args.root, apply=args.apply), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
