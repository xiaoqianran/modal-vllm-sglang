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
        before = None
        if target.exists():
            before = state.local_media_stamp(target)
            if (before is None or before[2] != artifact["size"]
                    or sha256_file(target) != artifact["sha256"]):
                raise FileExistsError("Local output differs from cloud artifact")
            fields["reused"] = True
        else:
            fields.update(download_volume_file(
                volume, volume_key("/" + artifact["path"]), target,
                expected_sha256=artifact["sha256"], kind=kind,
                refuse_existing=True, max_bytes=artifact["size"]))
        stamp = state.local_media_stamp(target)
        if stamp is None or stamp[2] != artifact["size"] or (before is not None and stamp != before):
            raise OSError("Local output changed before collection receipt")
        return {**artifact, "storage": "local", "file_stamp": stamp,
                "path": target.relative_to(state.media_directory(directory)).as_posix()}
