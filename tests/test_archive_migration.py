from unittest.mock import Mock, patch
from urllib.error import HTTPError

import pytest

from runtime.archive.migration import audit_references, migrate_receipt, migration_plan


def receipt():
    return {"status": "archived", "provider": "huggingface_bucket",
            "bucket": "example/assets", "path": "projects/old/image.webp",
            "stored_sha256": "a" * 64, "stored_bytes": 20}


def test_migration_dry_run_and_gc_protect_legacy():
    row = receipt()
    plan = migration_plan(row)
    assert plan["target"] == "assets/sha256/aa/" + "a" * 64 + ".webp"
    result = audit_references([row, row], row["bucket"])
    assert result["known_references"] == {plan["source"]: 2, plan["target"]: 2}
    assert result["deletion_allowed"] is False


def test_migration_server_copy_and_partial_success():
    row = receipt()
    provider = Mock(bucket=row["bucket"], token="token")
    provider.verify_digest.side_effect = ["sha256", HTTPError("https://example.invalid", 404, "", {}, None),
                                          "sha256", "sha256"]
    metadata = Mock(size=20)
    metadata.xet_file_data.file_hash = "xet"
    with patch("huggingface_hub.get_bucket_file_metadata", return_value=metadata), \
         patch("huggingface_hub.batch_bucket_files", side_effect=OSError("partial")) as copy:
        result = migrate_receipt(provider, row)
    copy.assert_called_once_with(row["bucket"], copy=[("bucket", row["bucket"], "xet", result["target"])],
                                 token="token")
    assert result["status"] == "copied"


def test_migration_never_overwrites_corruption():
    row = receipt()
    provider = Mock(bucket=row["bucket"])
    provider.verify_digest.side_effect = ["sha256", RuntimeError("SHA256 mismatch")]
    with patch("huggingface_hub.batch_bucket_files") as copy:
        with pytest.raises(RuntimeError, match="SHA256"):
            migrate_receipt(provider, row)
    copy.assert_not_called()
