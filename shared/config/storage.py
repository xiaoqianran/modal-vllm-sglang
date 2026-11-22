"""Storage identity shared by offline planning, clients and cloud deployments."""
import os

VOLUME_NAME = "modal-vllm-sglang"
SHARED_VOLUME_NAME = "modal-model-store-v2"
SHARED_MOUNT = "/mnt/models"


def shared_volume_name():
    """Allow isolated test environments without changing production identifiers."""
    return os.getenv("MODEL_SHARED_VOLUME_NAME", SHARED_VOLUME_NAME)


def runtime_volume_name():
    """Keep production writable metadata and pilot metadata completely isolated."""
    return os.getenv("MODEL_RUNTIME_VOLUME_NAME", VOLUME_NAME)


def shared_storage(spec):
    """Only explicitly opted-in HF services use the shared immutable model store."""
    return spec.get("model_storage", "legacy") == "shared"
