"""Original-only archive microbatch. Derived media is never on this path."""
from dataclasses import replace
from tempfile import TemporaryDirectory
from contextlib import nullcontext
from pathlib import Path
from urllib.error import HTTPError
from threading import Lock

from runtime.archive.huggingface import HuggingFaceBucket, public_url
from runtime.archive.operations import describe, prepare_media, verified_volume_copy
from runtime.archive.encoding_index import lookup_encoding, mapping_path, mapping_bytes
from runtime.archive.pipeline import asset_path
from runtime.delivery.cloud_cpu import mounted_source
from runtime.delivery.outbox import MAX_FILES, MAX_BYTES, classify, retry_after
from runtime.utils.files import sha256_file


class ObservedBucket(HuggingFaceBucket):
    """Preserve SDK error classification through non-transactional readback."""
    upload_error = None
    on_verified = None
    submitted = frozenset()

    def verify(self, local_file, remote_path, mode, *, expected_sha256=None):
        result = super().verify(local_file, remote_path, mode, expected_sha256=expected_sha256)
        if self.on_verified is not None and result == 'sha256':
            self.on_verified(remote_path, (public_url(self.bucket, remote_path), result,
                                           remote_path not in self.submitted))
        return result

    def upload_many(self, entries, *, indexes=None):
        self.submitted = frozenset(entries)
        try:
            return super().upload_many(entries, indexes=indexes)
        except Exception as exc:
            self.upload_error = exc
            raise


def error_row(exc):
    return {"error": type(exc).__name__, "kind": classify(exc), "retry_after": retry_after(exc)}


def publish_originals(root, requests, *, metrics=None, provider_factory=ObservedBucket, on_receipt=None, workspace=None, local_inputs=None):
    if not 1 <= len(requests) <= MAX_FILES:
        raise ValueError("Invalid durable archive batch count")
    if len(requests)>1 and sum(r["artifact"]["size"] for r in requests)>MAX_BYTES:
        raise ValueError("Durable archive byte limit exceeded")
    buckets={r["delivery"]["bucket"] for r in requests}
    if len(buckets)!=1:
        raise ValueError("Mixed archive buckets")
    bucket=next(iter(buckets))
    provider=provider_factory(bucket)
    metrics={} if metrics is None else metrics
    results=[None]*len(requests)
    entries,mappings,prepared,cache,outcomes={},{},{},{},{}
    with (TemporaryDirectory(prefix="hf-originals-") if workspace is None else nullcontext(workspace)) as work:
        from runtime.archive.works import check_original_slot, media_path, publish_task
        published_tasks = {}
        for i,req in enumerate(requests):
            try:
                artifact,settings=req["artifact"],req["delivery"]
                work_job = req.get("work")
                if work_job and work_job["id"] not in published_tasks:
                    publish_task(provider, work_job["id"], req["work_task"],
                                 Path(work) / "definitions" / work_job["id"])
                    published_tasks[work_job["id"]] = req["work_task"]
                if work_job and published_tasks[work_job["id"]] != req["work_task"]:
                    raise ValueError("Conflicting work task definition")
                source=mounted_source(root,artifact)
                if local_inputs is not None:
                    if workspace is None:
                        raise ValueError("Retained media requires caller-owned workspace")
                    snapshot = Path(work) / str(i) / "source" / source.name
                    snapshot.parent.mkdir(parents=True, exist_ok=True)
                    if not verified_volume_copy(root, {"source_artifact": artifact,
                            "stored_sha256": artifact["sha256"], "stored_bytes": artifact["size"]}, snapshot):
                        raise ValueError("Source snapshot integrity mismatch")
                    source = snapshot
                key=(artifact["sha256"],artifact["size"],source.suffix.lower(),settings["compression"],settings["min_saving"],bool(work_job))
                if key in cache:
                    if local_inputs is None and sha256_file(source)!=artifact["sha256"]:
                        raise ValueError("Source checksum mismatch")
                    media=replace(cache[key],source=source)
                else:
                    media=prepare_media(source,Path(work)/str(i),settings["compression"],settings["min_saving"],
                                        expected_sha256=artifact["sha256"],
                                        _verified_snapshot=local_inputs is not None,
                                        lookup=None if work_job else lambda m,s:lookup_encoding(provider,m,s))
                    cache[key]=media
                remote=media_path(work_job, media.upload_file.suffix) if work_job else asset_path(media)
                if work_job and settings["compression"] == "adaptive_lossless":
                    check_original_slot(provider, work_job, remote)
                if media.remote_only:
                    outcomes[remote]=(public_url(bucket,remote),"sha256",True)
                else:
                    entries.setdefault(remote,(media.upload_file,media.stored_sha256))
                    if not work_job and source.suffix.lower()==".png" and settings["compression"]=="adaptive_lossless":
                        mappings.setdefault(remote,{})[mapping_path(media,settings["min_saving"])]=mapping_bytes(media)
                prepared[i]=(media,remote,artifact)
            except Exception as exc:
                results[i]=error_row(exc)
        slots = {}
        for i, (media, remote, _) in prepared.items():
            work_job = requests[i].get("work")
            if work_job:
                slot = (work_job["id"], work_job["item"], work_job["step"])
                slots.setdefault(slot, []).append(i)
        for group in slots.values():
            signatures = {(prepared[i][1], prepared[i][0].original_sha256,
                           prepared[i][0].stored_sha256) for i in group}
            if len(signatures) == 1:
                continue
            # Reject every conflicting row before uploading any bytes for this
            # slot. Other independent work slots in the batch can still finish.
            for i in group:
                _, remote, _ = prepared.pop(i)
                entries.pop(remote, None)
                mappings.pop(remote, None)
                outcomes.pop(remote, None)
                results[i] = error_row(ValueError("Conflicting originals for one work step"))
        entries={k:v for k,v in entries.items() if k not in outcomes}
        seen=set()
        lock=Lock()

        def finish(i, outcome):
            if results[i] is not None:
                return
            media,remote,artifact=prepared[i]
            if isinstance(outcome,Exception):
                # Preserve authentication/rate errors on a failed batch readback.
                upload_error=getattr(provider,"upload_error",None)
                if isinstance(outcome,HTTPError) and outcome.code==404 and upload_error is not None:
                    outcome=upload_error
                results[i]=error_row(outcome)
                return
            url,verification,reused=outcome
            if verification!="sha256":
                results[i]=error_row(ValueError("Unverified archive outcome"))
                return
            row=describe(media,bucket,remote,url,verification,"archived")
            row.update(reused=reused or remote in seen,source_artifact=artifact)
            seen.add(remote)
            results[i]={"receipt":row}
            if local_inputs is not None:
                local = media.upload_file
                if media.remote_only:
                    local = media.source if media.original_sha256 == media.stored_sha256 else None
                if local is not None:
                    local_inputs[(bucket, row["stored_sha256"], row["stored_bytes"])] = local
            if on_receipt is not None:
                on_receipt(i,row)

        def verified(remote,outcome):
            with lock:
                for i,(_,path,_) in prepared.items():
                    if path==remote:
                        finish(i,outcome)

        if on_receipt is not None:
            for remote,outcome in outcomes.items():
                verified(remote,outcome)
        if entries:
            try:
                if on_receipt is not None and isinstance(provider, ObservedBucket):
                    provider.on_verified=verified
                outcomes.update(provider.publish_many(entries,metrics=metrics,
                                                      indexes={k:v for k,v in mappings.items() if k in entries}))
            except Exception as exc:
                outcomes.update(dict.fromkeys(entries,exc))
        for i,(_,remote,_) in prepared.items():
            finish(i,outcomes[remote])
    metrics["archived"]=sum("receipt" in r for r in results)
    return results
