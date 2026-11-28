"""Regression tests for atomic state publication on Windows."""
from unittest.mock import patch

import pytest

from runtime.utils import files


def sharing_error():
    exc = PermissionError("simulated Windows sharing violation")
    exc.winerror = 5
    return exc


def test_atomic_write_retries_transient_sharing_violation(tmp_path):
    target = tmp_path / "state.json"
    target.write_bytes(b"old")
    real_replace = files.os.replace
    attempts = []

    def replace(source, destination):
        attempts.append(1)
        assert target.read_bytes() == b"old"
        if len(attempts) < 3:
            raise sharing_error()
        real_replace(source, destination)

    with patch.object(files.os, "replace", side_effect=replace), patch.object(files.time, "sleep"):
        files.atomic_write(target, b"new")
    assert len(attempts) == 3
    assert target.read_bytes() == b"new"
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_write_persistent_failure_preserves_old_file(tmp_path):
    target = tmp_path / "state.json"
    target.write_bytes(b"old")
    with patch.object(files.os, "replace", side_effect=sharing_error()) as replace, patch.object(files.time, "sleep"):
        with pytest.raises(PermissionError):
            files.atomic_write(target, b"new")
    assert replace.call_count == 7
    assert target.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_write_unrelated_error_is_not_retried(tmp_path):
    target = tmp_path / "state.json"
    with patch.object(files.os, "replace", side_effect=OSError("disk full")) as replace:
        with pytest.raises(OSError):
            files.atomic_write(target, b"new")
    assert replace.call_count == 1
    assert not target.exists()
    assert not list(tmp_path.iterdir())
