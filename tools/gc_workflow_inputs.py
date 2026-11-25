"""Preview and explicitly collect retired, dependency-checked workflow inputs."""
import argparse
import json
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestration import storage_gc, workflow_recovery
from runtime.utils.files import atomic_json, read_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "retire", "apply"):
        p = sub.add_parser(command)
        p.add_argument("--volume", required=True)
        if command == "plan":
            p.add_argument("--out", required=True)
            p.add_argument("--min-age-seconds", type=int, default=storage_gc.DEFAULT_AGE)
        elif command == "retire":
            p.add_argument("--id", required=True)
            p.add_argument("--confirm-release-inputs", action="store_true")
        else:
            p.add_argument("--plan", required=True)
            p.add_argument("--confirm-quiescent", action="store_true",
                           help="All local runners, cloud controllers and other users of this namespace are stopped")
    args = parser.parse_args(argv)
    import modal
    volume = modal.Volume.from_name(args.volume, version=2)
    volume.hydrate()
    if args.command == "plan":
        result = storage_gc.plan_gc(volume, args.volume, min_age=args.min_age_seconds)
        atomic_json(args.out, result)
        print(json.dumps({"report": str(Path(args.out).resolve()), "roots": len(result["roots"]),
                          "legacy_roots": len(result["legacy_state_only_roots"]),
                          "candidates": len(result["candidates"]), "candidate_bytes": result["candidate_bytes"],
                          "deletion_allowed": result["deletion_allowed"], "errors": result["errors"]},
                         ensure_ascii=False, indent=2))
    elif args.command == "retire":
        manifest = workflow_recovery.load_plan(volume, args.id, args.volume)
        result = storage_gc.retire(volume, manifest, modal, confirm=args.confirm_release_inputs)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        result = storage_gc.apply_gc(volume, args.volume, read_json(Path(args.plan)),
                                     confirm_quiescent=args.confirm_quiescent)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    main()
