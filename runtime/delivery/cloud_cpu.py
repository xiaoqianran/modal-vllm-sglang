"""HF asset publishing inside a Modal CPU container, reading a mounted Volume.

This module is pure Python and can be tested without Modal or network access.
The caller sends only artifact metadata and delivery settings, never file bytes.
"""
from pathlib import Path
import re

from runtime.archive import ArchivePipeline, ArchiveSettings
from runtime.utils.media import media_suffix


def mounted_source(root, artifact):
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
    if path.stat().st_size != artifact["size"]:
        raise ValueError("Cloud Volume artifact checksum/size mismatch")
    with path.open("rb") as source:
        suffix = media_suffix(source.read(32), artifact["type"])
    if path.suffix.lower().replace(".jpeg", ".jpg") != suffix:
        raise ValueError("Artifact extension differs from media content")
    return path


def archive_mounted_artifact(root, artifact, settings, project):
    path = mounted_source(root, artifact)
    pipeline = ArchivePipeline(ArchiveSettings(
        bucket=settings["bucket"], project=project,
        compression=settings["compression"], verify=settings["verify"],
        min_saving=settings["min_saving"]))
    result = pipeline.run(path, upload=True, expected_sha256=artifact["sha256"])
    return {**result, "source_artifact": artifact}


def archive_mounted_batch(root, requests, *, metrics=None):
    """Bounded per-bucket batch with independent receipts and no source deletion."""
    from dataclasses import replace
    from tempfile import TemporaryDirectory
    from time import perf_counter
    from runtime.archive.config import ARCHIVE_BATCH_BYTES, ARCHIVE_BATCH_FILES
    from runtime.archive.huggingface import HuggingFaceBucket, public_url
    from runtime.archive.operations import describe, prepare_media
    from runtime.archive.encoding_index import lookup_encoding, mapping_path, mapping_bytes
    from runtime.archive.pipeline import asset_path
    from runtime.archive.preview import prepare_preview, lookup_preview, preview_index_path, preview_mapping
    from runtime.delivery.config import normalize_delivery
    from runtime.utils.files import sha256_file
    if not isinstance(requests, list) or not 1 <= len(requests) <= ARCHIVE_BATCH_FILES:
        raise ValueError("Invalid archive batch count")
    # Reject mixed buckets and oversize multi-file payloads at the remote boundary.
    if len(requests) > 1 and sum(r["artifact"]["size"] for r in requests) > ARCHIVE_BATCH_BYTES:
        raise ValueError("Archive batch byte limit exceeded")
    buckets = {r["delivery"]["bucket"] for r in requests}
    if len(buckets) != 1:
        raise ValueError("Archive batch must use one bucket")
    metrics = {} if metrics is None else metrics
    metrics.update(requests=len(requests), source_hits=0, encoding_hits=0,
                   upload_calls=0, index_upload_calls=0, sdk_upload_calls=0, submitted_bytes=0)
    started = perf_counter()
    results = [None] * len(requests)
    prepared = {}
    by_source = {}
    entries = {}
    cached_outcomes = {}
    mappings = {}
    previews = {}
    cached_previews = {}
    bucket = next(iter(buckets))
    provider = None
    def error(exc):
        return {"error": type(exc).__name__ + ": archive failed; source retained"}

    with TemporaryDirectory(prefix="hf-batch-") as work:
        for i, request in enumerate(requests):
            try:
                artifact = request["artifact"]
                settings = normalize_delivery(request["delivery"])
                if settings["provider"] not in {"huggingface", "both"}:
                    raise ValueError("HF delivery required")
                path = mounted_source(root, artifact)
                if provider is None:
                    provider = HuggingFaceBucket(bucket)
                key = (artifact["sha256"], artifact["size"], path.suffix.lower(),
                       settings["compression"], settings["min_saving"])
                if key in by_source:
                    # Distinct Volume paths must independently prove their bytes.
                    if sha256_file(path) != artifact["sha256"]:
                        raise ValueError("Source artifact checksum mismatch")
                    media = replace(by_source[key], source=path)
                    metrics["source_hits"] += 1
                else:
                    media = prepare_media(path, Path(work) / str(i), settings["compression"],
                                          settings["min_saving"], expected_sha256=artifact["sha256"],
                                          lookup=lambda original, saving: lookup_encoding(provider, original, saving))
                    if media.remote_only:
                        metrics["encoding_hits"] += 1
                    by_source[key] = media
                remote = asset_path(media)
                if media.remote_only:
                    cached_outcomes[remote] = (public_url(bucket, remote), "sha256", True)
                else:
                    entries.setdefault(remote, (media.upload_file, media.stored_sha256))
                    if (media.source.suffix.lower() == ".png"
                            and settings["compression"] == "adaptive_lossless"):
                        mappings.setdefault(remote, {})[mapping_path(media, settings["min_saving"])] = mapping_bytes(media)
                if media.kind == "image" and remote not in previews:
                    try:
                        # Lookup only on a verified original encoding cache hit; new
                        # originals avoid an extra negative HTTP lookup.
                        cached = lookup_preview(provider, media.stored_sha256, media.stored_size) if media.remote_only else None
                        if cached:
                            cached_previews[remote] = cached
                            previews[remote] = None
                            metrics["preview_hits"] = metrics.get("preview_hits", 0) + 1
                        else:
                            previews[remote] = prepare_preview(path, Path(work) / str(i), media.stored_size)
                            if previews[remote]:
                                preview_file, preview = previews[remote]
                                entries.setdefault(preview["path"], (preview_file, preview["sha256"]))
                                preview = {**preview, "url": public_url(bucket, preview["path"]), "verification": "sha256"}
                                mappings.setdefault(preview["path"], {})[preview_index_path(media.stored_sha256)] = preview_mapping(media.stored_sha256, media.stored_size, preview)
                    except Exception:
                        # Preview failures must never prevent original delivery.
                        previews[remote] = None
                        metrics["preview_errors"] = metrics.get("preview_errors", 0) + 1
                prepared[i] = (media, remote, artifact, settings)
            except Exception as exc:
                results[i] = error(exc)
        metrics.update(prepare_ms=round((perf_counter() - started)*1000, 2),
                       unique_objects=len(set(entries) | set(cached_outcomes)))
        if prepared:
            entries = {path: entry for path, entry in entries.items() if path not in cached_outcomes}
            outcomes = dict(cached_outcomes)
            preview_paths = {candidate[1]["path"] for candidate in previews.values() if candidate}
            if entries:
                indexes = {path: records for path, records in mappings.items() if path in entries}
                try:
                    if indexes:
                        outcomes.update(provider.publish_many(entries, metrics=metrics, indexes=indexes))
                    else:
                        outcomes.update(provider.publish_many(entries, metrics=metrics))
                except Exception as exc:
                    outcomes.update(dict.fromkeys(entries, exc))
                # A preview is optional. Retry failed originals independently so
                # a preview upload or batch transport failure cannot block delivery.
                original_entries = {path: entry for path, entry in entries.items()
                                    if path not in preview_paths}
                failed_originals = {path: entry for path, entry in original_entries.items()
                                    if isinstance(outcomes.get(path), Exception)}
                if failed_originals:
                    metrics["original_retry_count"] = metrics.get("original_retry_count", 0) + len(failed_originals)
                    retry_indexes = {path: records for path, records in mappings.items()
                                     if path in failed_originals}
                    try:
                        if retry_indexes:
                            outcomes.update(provider.publish_many(failed_originals, metrics=metrics,
                                                                  indexes=retry_indexes))
                        else:
                            outcomes.update(provider.publish_many(failed_originals, metrics=metrics))
                    except Exception as exc:
                        outcomes.update(dict.fromkeys(failed_originals, exc))
                    for path, (file, digest) in failed_originals.items():
                        if isinstance(outcomes.get(path), Exception):
                            try:
                                outcomes[path] = provider.publish(file, path, "sha256",
                                                                  expected_sha256=digest)
                            except Exception as exc:
                                outcomes[path] = exc
            metrics["verified_encoding_hits"] = len(cached_outcomes)
            seen = set()
            for i, (media, remote, artifact, settings) in prepared.items():
                outcome = outcomes[remote]
                if isinstance(outcome, Exception):
                    results[i] = error(outcome)
                    continue
                url, verification, reused = outcome
                receipt = describe(media, bucket, remote, url, verification, "archived")
                receipt.update(reused=reused or remote in seen, source_artifact=artifact)
                if remote in cached_previews:
                    receipt["preview"] = cached_previews[remote]
                candidate = previews.get(remote)
                if candidate:
                    _, preview = candidate
                    result = outcomes.get(preview["path"])
                    if isinstance(result, tuple) and result[1] == "sha256":
                        receipt["preview"] = {**preview, "url": result[0], "verification": result[1]}
                    else:
                        metrics["preview_errors"] = metrics.get("preview_errors", 0) + 1
                results[i] = {"receipt": receipt}
                seen.add(remote)
    metrics["archived"] = sum("receipt" in row for row in results)
    return results
