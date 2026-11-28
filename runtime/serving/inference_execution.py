"""Inference execution, durable result publication and recovery."""
import hashlib
import time
from pathlib import Path

from runtime.tasks.result_integrity import MEDIA_SUFFIXES, validate_media_record, verify_local_media
from runtime.utils.files import atomic_json, read_json, sha256_file
from runtime.utils.media import media_suffix
from runtime.utils.outputs import validate_path_component
from runtime.tasks.task_lifecycle import recovery_action, TERMINAL, SubmissionRejected
from runtime.telemetry.recorder import Recorder

from runtime.tasks.task_spec import prepare, execution_signature


def execute(spec, items, prefix, volume, cache_fingerprint="unverified", recorder=None,
            background_telemetry=False, ensure_engine=None, result_writer=None):
    from runtime.serving.inference_api import ImageGenerationClient, SGLangVideoClient
    parts = prefix.split("/")
    for part in parts:
        validate_path_component(part)
    folder = Path("/data").joinpath(*parts)
    prepared = [(item, *prepare(spec, item)) for item in items]
    if not prepared or len({x[0]["id"].lower() for x in prepared}) != len(prepared):
        raise ValueError("Nonempty batch with unique IDs required")
    port = spec.get("proxy_port", spec.get("port", 8000) + 1)
    client = (ImageGenerationClient(f"http://127.0.0.1:{port}", **({"output_directory": spec["image_output_directory"]} if spec.get("image_output_directory") else {}))
              if spec["kind"] == "image" else SGLangVideoClient(f"http://127.0.0.1:{port}"))
    if result_writer is not None and len(prepared) != 1:
        raise ValueError("Deferred publication requires one formal item")
    for item, payload, signature in prepared:
        context = item.get("_telemetry", {})
        telemetry = (recorder or Recorder()).bind(**{key: context[key] for key in ("task_id", "job_key") if key in context})
        started = time.perf_counter()
        commit_ms = {}

        def commit_checkpoint(label):
            before = time.perf_counter()
            try:
                volume.commit()
            finally:
                commit_ms[label] = (time.perf_counter() - before) * 1000

        metadata = folder / (item["id"] + ".json")
        old = read_json(metadata) if metadata.exists() else {}
        if old.get("intent_hash") not in (None, signature):
            raise ValueError("Remote task signature changed")
        execution_hash = execution_signature(signature, cache_fingerprint)
        if old and old.get("execution_hash") != execution_hash:
            raise ValueError("Remote model cache changed")
        if old.get("status") in {"collecting", "completed"} and old.get("intent_hash") != signature:
            raise ValueError("Remote task signature missing or changed")
        if old.get("status") == "completed":
            validate_media_record(item, old)
            target = folder / old["file"]
            if not verify_local_media(target, old):
                raise ValueError("Remote result damaged")
            yield old
            continue
        if old.get("status") == "collecting":
            validate_media_record(item, old)
            target = folder / old["file"]
            if verify_local_media(target, old):
                old["status"] = "completed"
                atomic_json(metadata, old)
                volume.commit()
                yield old
                continue
        if not old and any((folder / (item["id"] + suffix)).exists() for suffix in MEDIA_SUFFIXES):
            raise FileExistsError("Unverified remote media already exists")
        action = recovery_action(old)
        if action == "stop":
            raise RuntimeError("Remote task failed; create a new task to generate again")
        if action == "uncertain":
            raise RuntimeError("Uncertain submission; inspect remote metadata before creating a new task")
        source = None
        if action != "resume" and "image_artifact" in payload:
            from runtime.tasks.artifacts import read_image
            with telemetry.span("transfer", operation="read_edit_original"):
                source = read_image(payload.pop("image_artifact"), volume, Path("/data"))
        if action != "resume" and any(key.endswith("_artifact") for key in payload):
            from runtime.tasks.artifacts import resolve_frames
            with telemetry.span("transfer", operation="read_frames"):
                payload = resolve_frames(payload, volume, mount_root=Path("/data"))
        # Existing verified results and uncertain submissions never start the engine.
        # Initialization failure must not leave a false submitting checkpoint.
        if ensure_engine is not None:
            ensure_engine()
        if action == "resume":
            job = old["job_id"]
        else:
            # Persist before submit: a lost response never causes automatic resubmission.
            atomic_json(metadata, {"status": "submitting", "intent_hash": signature, "execution_hash": execution_hash})
            commit_checkpoint("submitting")
            try:
                if spec["kind"] == "image":
                    with telemetry.span("generate", operation="image"):
                        if source is not None:
                            data, _ = client.edit(payload, source, timeout=spec.get("timeout", 3600))
                        else:
                            data, _ = client.generate(payload, timeout=spec.get("timeout", 3600))
                else:
                    with telemetry.span("generate", operation="video_submit") as fields:
                        job = client.submit(payload, timeout=spec.get("timeout", 3600))
                        fields["engine_job_id"] = job
                    atomic_json(metadata, {"status": "running", "intent_hash": signature, "execution_hash": execution_hash, "job_id": job})
                    commit_checkpoint("running")
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
                client.wait(job, timeout=spec.get("timeout", 3600), poll=0.5, on_status=remember_failure)
            with telemetry.span("generate", operation="video_fetch"):
                data = client.content(job)
        # Keep oversized images and videos on the existing synchronous path;
        # the bounded writer must not introduce a new output-size restriction.
        if result_writer is not None and spec["kind"] == "image":
            from runtime.serving.preview_batch import MAX_ORIGINAL
        if result_writer is not None and spec["kind"] == "image" and len(data) <= MAX_ORIGINAL:
            def write(blob):
                from runtime.utils.files import atomic_write
                suffix = media_suffix(blob, "image")
                target = folder / (item["id"] + suffix)
                digest = hashlib.sha256(blob).hexdigest()
                if target.exists() and sha256_file(target) != digest:
                    raise FileExistsError(f"Refusing overwrite: {target}")
                row = {"id": item["id"], "file": target.name, "status": "collecting",
                       "bytes": len(blob), "sha256": digest,
                       "execution_seconds": time.perf_counter() - started,
                       "intent_hash": signature, "execution_hash": execution_hash}
                if telemetry.writer is not None:
                    row["telemetry"] = telemetry.writer.reference()
                atomic_json(metadata, row)
                if not target.exists():
                    atomic_write(target, blob)
                def finish():
                    row["status"] = "completed"
                    atomic_json(metadata, row)
                return row, finish
            yield result_writer.enqueue_publication(data, write)
            continue
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
        with telemetry.span("save", bytes=len(data)) as fields:
            fields["commit_ms"] = commit_ms
            atomic_json(metadata, record)
            commit_checkpoint("collecting")
            from runtime.utils.files import atomic_write
            if not target.exists():
                atomic_write(target, data)
            record["status"] = "completed"
            atomic_json(metadata, record)
            commit_checkpoint("completed")
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
