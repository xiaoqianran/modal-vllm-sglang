"""One Modal Volume V2 for model weights, caches and generated artifacts."""
import os

VOLUME_NAME = "model-store"
VOLUME_VERSION = 2
VOLUME_MOUNT = "/data"
VOLUME_SUB_PATH = "/inference"


def volume_key(path):
    """Map a logical service-relative path to the raw Volume SDK namespace."""
    from pathlib import PurePosixPath
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("Volume key must be absolute")
    key = PurePosixPath(path)
    if ".." in key.parts or "\\" in path:
        raise ValueError("Invalid Volume key")
    return VOLUME_SUB_PATH + key.as_posix()

SHARED_MOUNT = VOLUME_MOUNT  # Shared HF snapshots live inside the same Volume.


def runtime_volume_name():
    """Allow isolated test runs to select a different single Volume."""
    return os.getenv("MODEL_VOLUME_NAME", VOLUME_NAME)


def shared_volume_name():
    """Compatibility for shared snapshot tools; never create a second Volume."""
    return runtime_volume_name()


def shared_storage(spec):
    """Shared snapshot layout is optional, but always in the same Volume."""
    return spec.get("model_storage") == "shared"
