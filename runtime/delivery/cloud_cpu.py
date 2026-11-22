"""HF asset publishing inside a Modal CPU container, reading a mounted Volume.

This module is pure Python and can be tested without Modal or network access.
The caller sends only artifact metadata and delivery settings, never file bytes.
"""
from pathlib import Path
import re

from runtime.archive import ArchivePipeline, ArchiveSettings
from runtime.utils.files import sha256_file
from runtime.utils.media import media_suffix


def archive_mounted_artifact(root, artifact, settings, project):
    if not isinstance(artifact, dict) or artifact.get("storage") != "volume":
        raise ValueError("Cloud archive requires a Modal Volume artifact")
    relative = artifact.get("path")
    if (not isinstance(relative, str)
            or not re.fullmatch(r"model-results/[A-Za-z0-9_.\-/]+", relative)
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or "\\" in relative):
        raise ValueError("Invalid Modal Volume artifact path")
    if artifact.get("type") not in {"image", "video"}:
        raise ValueError("Unsupported artifact type")
    if (type(artifact.get("size")) is not int or artifact["size"] <= 0
            or not isinstance(artifact.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"])):
        raise ValueError("Invalid artifact integrity metadata")
    mount = Path(root).resolve()
    # The Modal Volume is mounted at the logical results root (sub_path applied).
    data_root = mount
    path = (data_root / relative).resolve()
    if not path.is_relative_to(data_root) or not path.is_file():
        raise FileNotFoundError("Committed artifact missing in cloud Volume")
    if path.stat().st_size != artifact["size"] or sha256_file(path) != artifact["sha256"]:
        raise ValueError("Cloud Volume artifact checksum/size mismatch")
    with path.open("rb") as source:
        media_suffix(source.read(32), artifact["type"])
    pipeline = ArchivePipeline(ArchiveSettings(
        bucket=settings["bucket"], project=project,
        compression=settings["compression"], verify=settings["verify"],
        min_saving=settings["min_saving"]))
    result = pipeline.run(path, upload=True)
    return {**result, "source_artifact": artifact}
