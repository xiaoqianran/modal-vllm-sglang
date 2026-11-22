"""Optional HF delivery from verified Modal Volume bytes, never from outputs/."""
import tempfile
from pathlib import Path

from orchestration import model_capabilities
from runtime.archive import ArchivePipeline, ArchiveSettings
from runtime.utils.files import download_volume_file


def archive_from_volume(volume, job, record, plan):
    """CPU-only worker operation. Original Modal Volume object is untouched.

    A temporary *worker-side* copy is used for compression. No outputs/ file
    is created; a dedicated Modal CPU archiver can replace this adapter later.
    """
    kind = model_capabilities.CAPABILITIES[job["uses"]]["output"]
    artifact = record["artifacts"][kind]
    suffix = Path(record["result"]["file"]).suffix
    settings = plan["delivery"]
    with tempfile.TemporaryDirectory(prefix="volume-to-hf-") as folder:
        source = Path(folder) / ("source" + suffix)
        transfer = download_volume_file(
            volume, "/" + artifact["path"], source,
            expected_sha256=artifact["sha256"], max_bytes=artifact["size"],
            refuse_existing=True, kind=kind)
        if source.stat().st_size != artifact["size"]:
            raise ValueError("Modal Volume output size mismatch")
        pipeline = ArchivePipeline(ArchiveSettings(
            bucket=settings["bucket"], project=plan["project"],
            compression=settings["compression"],
            min_saving=settings["min_saving"], verify=settings["verify"]))
        archived = pipeline.run(source, upload=True)
        return {**archived, "source_artifact": artifact,
                "transfer_bytes": transfer["downloaded_bytes"]}
