"""Audit known workflow input references without deleting or reading media."""
import argparse
import json
from pathlib import Path
import sys

# Support both module and direct-script invocation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestration.input_audit import audit
from runtime.utils.files import atomic_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--volume", required=True)
    parser.add_argument("--task-root", action="append", required=True,
                        help="Task plan tree, normally .runtime/tasks; repeat for other workspaces")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    import modal
    volume = modal.Volume.from_name(args.volume, version=2)
    result = audit(volume, args.volume, args.task_root)
    atomic_json(args.out, result)
    print(json.dumps({key: result[key] for key in (
        "volume", "plans_scanned", "referenced_bytes", "unobserved_bytes", "deletion_allowed", "limitation")}
        | {"objects": len(result["objects"]), "missing": len(result["missing_references"]),
           "errors": len(result["errors"]), "report": str(Path(args.out).resolve())},
        indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
