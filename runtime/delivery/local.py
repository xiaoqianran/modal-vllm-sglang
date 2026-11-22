"""Local, content-verified Modal Volume to outputs delivery."""
from pathlib import Path

from orchestration import model_capabilities, state
from runtime.tasks.task_identity import intent_digest
from runtime.config.storage import volume_key
from runtime.utils.files import download_volume_file, sha256_file


def download_to_local(volume, directory, job, record, fields):
    kind = model_capabilities.CAPABILITIES[job["uses"]]["output"]
    suffix = Path(record["result"]["file"]).suffix
    target = state.media_directory(directory) / (state.output_stem(job) + suffix)
    artifact = record["artifacts"][kind]
    with state.lock(directory, filename=".download-" + intent_digest(job["key"]) + ".lock"):
        if target.exists():
            if sha256_file(target) != artifact["sha256"]:
                raise FileExistsError("Local output differs from cloud artifact")
            fields["reused"] = True
        else:
            fields.update(download_volume_file(
                volume, volume_key("/" + artifact["path"]), target,
                expected_sha256=artifact["sha256"], kind=kind,
                refuse_existing=True, max_bytes=artifact["size"]))
    return {**artifact, "storage": "local",
            "path": target.relative_to(state.media_directory(directory)).as_posix()}
