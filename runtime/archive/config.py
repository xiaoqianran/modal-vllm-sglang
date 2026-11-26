"""Project-wide Hugging Face asset storage defaults."""

DEFAULT_ASSET_BUCKET = "seachen/modal-vllm-sglang-assets"
ASSET_APP_NAME = "modal-vllm-sglang-assets"


def archive_source_version():
    """Content identity of the CPU archive implementation, independent of host line endings."""
    from hashlib import sha256
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    files = sorted((root / "archive").glob("*.py")) + [root / "delivery/cloud_cpu.py",
                                                      root / "delivery/config.py",
                                                      root / "utils/files.py",
                                                      root / "utils/media.py"]
    digest = sha256()
    for file in files:
        digest.update(file.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(sha256(file.read_bytes().replace(b"\r\n", b"\n")).digest())
    return digest.hexdigest()


def assert_archive_version():
    import os
    actual = archive_source_version()
    if os.environ.get("ARCHIVE_DEPLOY_VERSION") != actual:
        raise RuntimeError("Archive source version mismatch; refusing publication")
    return actual


ARCHIVE_BATCH_FILES = 4
ARCHIVE_BATCH_BYTES = 64 * 1024 * 1024
ARCHIVE_BATCH_WAIT = 0.1
