"""Inference execution, durable result publication and recovery."""
import hashlib
import time
from pathlib import Path

from shared.tasks.batch import completed_output
from shared.utils.files import atomic_json, read_json, sha256_file
from shared.utils.media import media_suffix
from shared.utils.outputs import component
from shared.tasks.task_lifecycle import recovery_action, TERMINAL, SubmissionRejected
from shared.telemetry.recorder import Recorder

from shared.tasks.task_spec import prepare, execution_signature


def execute(spec, items, prefix, volume, cache_fingerprint="unverified", recorder=None, background_telemetry=False):
    from shared.serving.inference_api import VLLMImageClient, SGLangVideoClient
    parts = prefix.split("/")
    for part in parts:
        component(part)
    folder = Path("/mnt/modal").joinpath(*parts)
    prepared = [(item, *prepare(spec, item)) for item in items]
    if not prepared or len({x[0]["id"].lower() for x in prepared}) != len(prepared):
        raise ValueError("Nonempty batch with unique IDs required")
    port = spec.get("proxy_port", spec.get("port", 8000) + 1)
    client = (VLLMImageClient if spec["kind"] == "image" else SGLangVideoClient)(f"http://127.0.0.1:{port}")
    for item, payload, signature in prepared:
        context = item.get("_telemetry", {})
        telemetry = (recorder or Recorder()).bind(**{key: context[key] for key in ("task_id", "job_key") if key in context})
        started = time.perf_counter()
        metadata = folder / (item["id"] + ".json")
        old = read_json(metadata) if metadata.exists() else {}
        if old.get("intent_hash") not in (None, signature):
            raise ValueError("Remote task signature changed")
        execution_hash = execution_signature(signature, cache_fingerprint)
        if old and old.get("execution_hash") != execution_hash:
            raise ValueError("Remote model cache changed")
        if old.get("status") == "completed":
            if old.get("file") not in {item["id"] + s for s in (".png", ".jpg", ".webp", ".mp4")}:
                raise ValueError("Unsafe remote result filename")
            target = folder / old["file"]
            if not completed_output(target, metadata, signature):
                raise ValueError("Remote result damaged")
            yield old
            continue
        if old.get("status") == "collecting" and old.get("file") in {item["id"] + s for s in (".png", ".jpg", ".webp", ".mp4")}:
            target = folder / old["file"]
            if target.is_file() and sha256_file(target) == old.get("sha256"):
                old["status"] = "completed"
                atomic_json(metadata, old)
                volume.commit()
                yield old
                continue
        if not old and any((folder / (item["id"] + suffix)).exists() for suffix in (".png", ".jpg", ".webp", ".mp4")):
            raise FileExistsError("Unverified remote media already exists")
        action = recovery_action(old)
        if action == "stop":
            raise RuntimeError("Remote task failed; create a new task to generate again")
        if action == "uncertain":
            raise RuntimeError("Uncertain submission; inspect remote metadata before creating a new task")
        if action == "resume":
            job = old["job_id"]
        else:
            from shared.tasks.artifacts import resolve_frames
            if any(key.endswith("_artifact") for key in payload):
                with telemetry.span("transfer", operation="read_frames"):
                    payload = resolve_frames(payload, volume)
            # Persist before submit: a lost response never causes automatic resubmission.
            atomic_json(metadata, {"status": "submitting", "intent_hash": signature, "execution_hash": execution_hash})
            volume.commit()
            try:
                if spec["kind"] == "image":
                    with telemetry.span("generate", operation="image"):
                        data, _ = client.generate(payload, timeout=spec.get("timeout", 3600))
                else:
                    with telemetry.span("generate", operation="video_submit") as fields:
                        job = client.submit(payload, timeout=spec.get("timeout", 3600))
                        fields["engine_job_id"] = job
                    atomic_json(metadata, {"status": "running", "intent_hash": signature, "execution_hash": execution_hash, "job_id": job})
                    volume.commit()
            except SubmissionRejected:
                atomic_json(metadata, {"status": "pending", "intent_hash": signature, "execution_hash": execution_hash})
                volume.commit()
                raise
        if spec["kind"] == "video":
            telemetry = telemetry.bind(engine_job_id=str(job))
            def remember_failure(remote):
                if remote.get("status") in TERMINAL - {"completed"}:
                    record = read_json(metadata)
                    record["status"] = "failed"
                    atomic_json(metadata, record)
                    volume.commit()
            with telemetry.span("generate", operation="video_wait"):
                client.wait(job, timeout=spec.get("timeout", 3600), on_status=remember_failure)
            with telemetry.span("generate", operation="video_fetch"):
                data = client.content(job)
        suffix = media_suffix(data, spec["kind"])
        target = folder / (item["id"] + suffix)
        digest = hashlib.sha256(data).hexdigest()
        if target.exists() and sha256_file(target) != digest:
            raise FileExistsError(f"Refusing overwrite: {target}")
        record = {"id": item["id"], "file": target.name, "status": "collecting", "bytes": len(data),
                  "execution_seconds": time.perf_counter() - started,
                  "intent_hash": signature, "execution_hash": execution_hash, "sha256": digest}
        if spec["kind"] == "video":
            record["job_id"] = job
        if telemetry.writer is not None:
            record["telemetry"] = telemetry.writer.reference()
        with telemetry.span("save", bytes=len(data)):
            atomic_json(metadata, record)
            volume.commit()
            from shared.utils.files import atomic_write
            if not target.exists():
                atomic_write(target, data)
            record["status"] = "completed"
            atomic_json(metadata, record)
            volume.commit()
        publish_telemetry(telemetry, metadata, record, volume, background=background_telemetry)
        yield record


def publish_telemetry(recorder, metadata, record, volume, *, background=False):
    """Optional metadata publication cannot turn successful generation into failure."""
    if recorder.writer is None:
        return
    try:
        reference = record.get("telemetry") or recorder.writer.reference()
        if background:
            # Only the immutable manifest is written in the background. Never race
            # another job by rewriting generation metadata after releasing its lock.
            if not record.get("telemetry"):
                record["telemetry"] = reference
                atomic_json(metadata, record)
            recorder.writer.publish_async(reference, volume.commit)
            return
        record["telemetry"] = recorder.writer.publish(reference)
        atomic_json(metadata, record)
        volume.commit()
    except Exception as exc:
        record["telemetry_error"] = type(exc).__name__
