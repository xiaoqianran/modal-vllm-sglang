"""Resume verified HF archival for completed local-only gallery assets.

Dry run by default. Never delete local originals or change task state.
"""
import argparse
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote

from orchestration import state, studio
from runtime.archive.config import DEFAULT_ASSET_BUCKET
from runtime.archive.huggingface import HuggingFaceBucket
from runtime.archive.operations import prepare_media, describe
from runtime.archive.pipeline import asset_path
from runtime.archive.encoding_index import lookup_encoding, mapping_path, mapping_bytes
from runtime.utils.files import atomic_json, file_lock, read_json, sha256_file


def reconcile_task_states(root, receipts):
    """Mirror only checksum-matched archive receipts into completed task records."""
    root = Path(root).resolve()
    output = root / "outputs"
    preview_path = output / ".runtime/preview-index.json"
    preview_index = read_json(preview_path) if preview_path.exists() else {}
    changed = 0
    for state_path in sorted((root / ".runtime/tasks").glob("*/*/state.json")):
        with state.lock(state_path.parent, timeout=10):
            data = read_json(state_path)
            dirty = False
            for job in data.get("jobs", {}).values():
                artifact = job.get("local_artifact")
                if job.get("status") != "completed" or not isinstance(artifact, dict):
                    continue
                source = (state.media_directory(state_path.parent) / artifact["path"]).resolve()
                if not source.is_relative_to(output):
                    continue
                row = receipts.get(source.relative_to(output).as_posix())
                if not isinstance(row, dict):
                    continue
                if (row.get("status") != "archived" or row.get("verification") != "sha256"
                    or row.get("original_sha256") != artifact.get("sha256")
                    or row.get("original_bytes") != artifact.get("size")):
                    continue
                archive = dict(row)
                preview = preview_index.get(row.get("url"), {})
                if (preview.get("source_sha256") == row.get("stored_sha256")
                        and preview.get("status") == "preview"):
                    archive["preview"] = preview["preview"]
                if job.get("cloud_archive") != archive:
                    job["cloud_archive"] = archive
                    dirty = True
            if dirty:
                atomic_json(state_path, data)
                changed += 1
    return changed


def pending_assets(root, receipts):
    """Use recorded completed local artifacts, not untrusted HTML paths alone."""
    root = Path(root).resolve()
    output = root / "outputs"
    local = {unquote(a["url"]) for a in studio.collect(output, thumbs=False)["assets"]
             if a["storage"] == "local"}
    found = {}
    for path in sorted((root / ".runtime/tasks").glob("*/*/state.json")):
        for job in read_json(path)["jobs"].values():
            artifact = job.get("local_artifact")
            if job.get("status") != "completed" or not artifact:
                continue
            if artifact.get("storage") != "local":
                continue
            source = (state.media_directory(path.parent) / artifact["path"]).resolve()
            if not source.is_relative_to(output):
                continue
            rel = source.relative_to(output).as_posix()
            if rel not in local:
                continue
            prior = receipts.get(rel, {})
            if (prior.get("status") == "archived" and prior.get("verification") == "sha256"
                    and prior.get("original_sha256") == artifact["sha256"]):
                continue
            if not source.is_file() or source.stat().st_size != artifact["size"]:
                raise ValueError(f"Local source missing or size changed: {rel}")
            found[rel] = (source, artifact)
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--reconcile-only", action="store_true",
                        help="Mirror existing verified receipts into completed task states")
    args = parser.parse_args()
    root = Path.cwd()
    path = root / "outputs" / studio.RECEIPTS
    with file_lock(path.parent, filename=".local-history.lock"):
        receipts = read_json(path) if path.exists() else {}
        reconciled = reconcile_task_states(root, receipts) if args.reconcile_only else 0
        if args.reconcile_only:
            print("RECONCILED_TASK_STATES", reconciled, flush=True)
            return 0
        pending = pending_assets(root, receipts)
        print("PLAN", len(pending), "files", sum(x[1]["size"] for x in pending.values()), "bytes", flush=True)
        if not args.upload:
            return 0
        provider = HuggingFaceBucket(DEFAULT_ASSET_BUCKET)
        counts = Counter()
        # Keep preparation memory bounded, and checkpoint every small batch.
        items = list(pending.items())
        for offset in range(0, len(items), 2):
            with TemporaryDirectory(prefix="local-history-") as tmp:
                prepared, entries, indexes, cached = {}, {}, {}, {}
                for i, (rel, (source, artifact)) in enumerate(items[offset:offset+2]):
                    try:
                        media = prepare_media(source, Path(tmp)/str(i), "adaptive_lossless", .05,
                            expected_sha256=artifact["sha256"],
                            lookup=lambda original, saving: lookup_encoding(provider, original, saving))
                        remote = asset_path(media)
                        prepared[rel] = media, remote
                        if media.remote_only:
                            cached[remote] = (None, "sha256", True)
                        else:
                            entries.setdefault(remote, (media.upload_file, media.stored_sha256))
                            if source.suffix.lower() == ".png":
                                indexes.setdefault(remote, {})[mapping_path(media,.05)] = mapping_bytes(media)
                    except Exception as exc:
                        counts["error"] += 1
                        print("PREPARE_ERROR", rel, type(exc).__name__, flush=True)
                from runtime.archive.huggingface import public_url
                results = {key:(public_url(provider.bucket,key), value[1], value[2]) for key,value in cached.items()}
                entries = {key:value for key,value in entries.items() if key not in results}
                if entries:
                    try:
                        results.update(provider.publish_many(entries,indexes=indexes))
                    except Exception as exc:
                        results.update(dict.fromkeys(entries,exc))
                for rel,(media,remote) in prepared.items():
                    outcome = results.get(remote)
                    if not isinstance(outcome,tuple) or outcome[1] != "sha256":
                        counts["error"] += 1
                        print("ARCHIVE_ERROR",rel,type(outcome).__name__,flush=True)
                        continue
                    url,verification,reused = outcome
                    receipts[rel] = describe(media,provider.bucket,remote,url,verification,"archived")
                    receipts[rel]["reused"] = reused
                    counts["archived"] += 1
                    print("ARCHIVED",rel,media.stored_size,"bytes","reused",reused,flush=True)
                atomic_json(path,receipts)
                reconciled = reconcile_task_states(root, receipts)
                if reconciled:
                    print("RECONCILED_TASK_STATES", reconciled, flush=True)
                print("PROGRESS",dict(counts),flush=True)
        print("FINISHED",dict(counts),flush=True)
        return int(bool(counts["error"]))

if __name__ == "__main__":
    raise SystemExit(main())
