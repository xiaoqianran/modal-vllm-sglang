"""Contracts for scoped model-store paths and raw Modal SDK keys."""
import pytest
from runtime.config.storage import VOLUME_NAME, VOLUME_VERSION, VOLUME_MOUNT, VOLUME_SUB_PATH, volume_key


def test_storage_identity_and_scope():
    assert (VOLUME_NAME, VOLUME_VERSION, VOLUME_MOUNT, VOLUME_SUB_PATH) == (
        "model-store", 2, "/data", "/inference"
    )
    assert volume_key("/models/001-moss-image/model-ready.json") == (
        "/inference/models/001-moss-image/model-ready.json"
    )
    assert volume_key("/model-results/moss/project/shot.json") == (
        "/inference/model-results/moss/project/shot.json"
    )
    assert volume_key("/workflow-inputs/abc") == "/inference/workflow-inputs/abc"


@pytest.mark.parametrize("invalid", ["", "models/foo", "/../comfyui/hf", "/a/../../b", "/a\\b"])
def test_reject_unsafe_sdk_paths(invalid):
    with pytest.raises(ValueError):
        volume_key(invalid)
