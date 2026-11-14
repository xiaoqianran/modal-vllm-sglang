"""Content-addressed remote results and verified, non-overwriting local delivery."""
import json
from pathlib import Path

from orchestration.state import image_format, local_path, save, write
from shared.files import sha256_file


def remote_prefix(job):
    return f"session-results/{job['identity']}/{job['fingerprint']}"


def validate_media(data, output):
    if output == "image":
        return image_format(data)[0]
    if output == "video" and len(data) >= 32 and data[4:8] == b"ftyp":
        return ".mp4"
    if output == "audio" and data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return ".wav"
    raise ValueError(f"Unsupported or invalid generated {output}")


def validate_manifest(job, result):
    if (result.get("job_id") != job["id"] or result.get("fingerprint") != job["fingerprint"]
            or result.get("status") != "completed" or result.get("suffix") not in
            {".png", ".jpg", ".webp", ".mp4", ".wav"}
            or not isinstance(result.get("sha256"), str) or len(result["sha256"]) != 64
            or type(result.get("bytes")) is not int or result["bytes"] <= 0):
        raise ValueError(f"Invalid result manifest for {job['identity']}")


def remote_manifest(volume, job):
    try:
        result = json.loads(b"".join(volume.read_file('/' + remote_prefix(job) + '/result.json')))
    except FileNotFoundError:
        return None
    validate_manifest(job, result)
    return result


def local_manifest_path(root, job):
    return local_path(root, f"outputs/{job['project']}/{job['scene']}/.runner/sessions/{job['shot']}.json")


def local_result(root, job):
    base = local_path(root, f"outputs/{job['identity']}")
    files = [base.with_suffix(s) for s in (".png", ".jpg", ".jpeg", ".webp", ".mp4", ".wav")
             if base.with_suffix(s).exists()]
    manifest = local_manifest_path(root, job)
    if not files:
        return None
    if len(files) != 1 or not manifest.is_file():
        raise FileExistsError(f"Existing output has no session provenance (preserved): {base}")
    result = json.loads(manifest.read_text(encoding="utf-8"))
    validate_manifest(job, result)
    if files[0].suffix != result["suffix"] or files[0].stat().st_size != result["bytes"] or sha256_file(files[0]) != result["sha256"]:
        raise ValueError(f"Existing output failed verification: {files[0]}")
    return result


def fetch_result(root, volume, job, result):
    validate_manifest(job, result)
    base = local_path(root, f"outputs/{job['identity']}")
    target = base.with_suffix(result["suffix"])
    existing = local_result(root, job)
    if existing:
        if existing["sha256"] != result["sha256"]:
            raise ValueError("Local and remote result differ")
        return target
    # Publish manifest first. A crash then leaves metadata which can safely be
    # completed on the next fetch, rather than an unidentifiable image.
    import hashlib
    data = b"".join(volume.read_file('/' + remote_prefix(job) + '/output' + result["suffix"]))
    if (len(data) != result["bytes"] or hashlib.sha256(data).hexdigest() != result["sha256"]
            or validate_media(data, job["output"]) != result["suffix"]):
        raise ValueError("Downloaded artifact failed content/SHA256 validation")
    save(local_manifest_path(root, job), result)
    write(target, data)
    return target
