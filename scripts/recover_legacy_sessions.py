"""Read-only recovery of legacy detached-session outputs; never starts GPU."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from orchestration.legacy_session_artifacts import fetch_result, local_result


def recover(root=ROOT, *, fetch=False, volume=None):
    database = root / "outputs" / ".sessions" / "tasks.sqlite3"
    if not database.is_file():
        raise FileNotFoundError(f"Legacy session database not found: {database}")
    # Open SQLite in read-only mode; do not restart the old coordinator or worker.
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        records = conn.execute(
            "SELECT body, result FROM jobs WHERE status IN ('completed', 'remote_done') ORDER BY created"
        ).fetchall()
    downloaded = existing = missing = 0
    for body, raw_result in records:
        job = json.loads(body)
        if not raw_result:
            raise ValueError(f"Missing legacy result manifest: {job['identity']}")
        result = json.loads(raw_result)
        if local_result(root, job) is not None:
            existing += 1
            continue
        if not fetch:
            print(f"AVAILABLE_REMOTE {job['identity']}", flush=True)
            missing += 1
            continue
        if volume is None:
            import modal
            volume = modal.Volume.from_name("modal-forge")
        path = fetch_result(root, volume, job, result)
        print(f"RECOVERED {path}", flush=True)
        downloaded += 1
    print(f"LEGACY_SESSION_RESULTS recorded={len(records)} present={existing} "
          f"downloaded={downloaded} pending={missing}", flush=True)
    return {"recorded": len(records), "present": existing,
            "downloaded": downloaded, "pending": missing}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true",
                        help="Download verified remote files; no GPU inference")
    args = parser.parse_args()
    recover(fetch=args.fetch)
