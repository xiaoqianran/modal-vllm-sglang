"""Non-destructive migration of explicitly identified HF archive receipts."""
import re
from pathlib import PurePosixPath
from urllib.error import HTTPError

from .huggingface import public_url


def migration_plan(receipt):
    """Never guess content identity from an old URL or a Xet hash."""
    if receipt.get("status") != "archived" or receipt.get("provider") != "huggingface_bucket":
        raise ValueError("Expected a completed HF archive receipt")
    bucket, source = receipt["bucket"], receipt["path"]
    public_url(bucket, source)
    digest, size = receipt["stored_sha256"], receipt["stored_bytes"]
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Invalid stored SHA-256")
    if type(size) is not int or size <= 0:
        raise ValueError("Invalid stored size")
    suffix = PurePosixPath(source).suffix.lower().replace(".jpeg", ".jpg")
    if suffix not in {".png", ".jpg", ".webp", ".mp4"}:
        raise ValueError("Unsupported media extension")
    target = f"assets/sha256/{digest[:2]}/{digest}{suffix}"
    return {"bucket": bucket, "source": source, "target": target,
            "size": size, "sha256": digest, "old_url": public_url(bucket, source),
            "new_url": public_url(bucket, target)}


def migrate_receipt(provider, receipt):
    """Verify old and new bytes; copy by Xet identity without re-uploading.

    Keep the old object and all existing receipts/HTML unchanged.
    """
    from huggingface_hub import batch_bucket_files, get_bucket_file_metadata

    plan = migration_plan(receipt)
    bucket, source, target = plan["bucket"], plan["source"], plan["target"]
    size, digest = plan["size"], plan["sha256"]
    if provider.bucket != bucket:
        raise ValueError("Migration bucket mismatch")
    provider.verify_digest(source, size, digest)
    if source == target:
        return {**plan, "status": "already_canonical"}
    try:
        provider.verify_digest(target, size, digest)
    except HTTPError as exc:
        if exc.code != 404:
            raise
    else:
        return {**plan, "status": "reused"}
    meta = get_bucket_file_metadata(bucket, source, token=provider.token)
    if meta.size != size or not meta.xet_file_data or not meta.xet_file_data.file_hash:
        raise RuntimeError("Source Xet identity unavailable")
    # Recheck the source against the receipt before issuing a server-side copy.
    provider.verify_digest(source, size, digest)
    current = get_bucket_file_metadata(bucket, source, token=provider.token)
    if current.size != size or not current.xet_file_data or current.xet_file_data.file_hash != meta.xet_file_data.file_hash:
        raise RuntimeError("Source changed during migration")
    try:
        batch_bucket_files(bucket, copy=[("bucket", bucket, meta.xet_file_data.file_hash, target)],
                           token=provider.token)
    except Exception:
        # Non-transactional copy may have succeeded; verify before reporting.
        pass
    provider.verify_digest(target, size, digest)
    return {**plan, "status": "copied"}


def audit_references(receipts, bucket):
    """Read-only known-root inventory; absence is never evidence of safe deletion."""
    references = {}
    for receipt in receipts:
        plan = migration_plan(receipt)
        if plan["bucket"] != bucket:
            raise ValueError("Mixed bucket inventory")
        for path in {plan["source"], plan["target"]}:
            references[path] = references.get(path, 0) + 1
    return {"bucket": bucket, "known_references": references,
            "complete": False, "deletion_allowed": False}
