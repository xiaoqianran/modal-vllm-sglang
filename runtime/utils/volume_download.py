"""Resumable, ordered-hash Modal Volume V2 downloads.

Only the transport adapter uses SDK internals (VolumeGetFile2 and block digest
verification). A verified artifact is published atomically; a failed transfer
keeps verified blocks and never changes the existing output.
"""
import asyncio
import hashlib
import os
import time
import threading
from pathlib import Path

from runtime.utils.files import atomic_json, read_json, file_lock

BLOCK_CONCURRENCY = 3
BLOCK_ATTEMPTS = 3
BLOCK_TIMEOUT = 60
RPC_TIMEOUT = 20


async def _recover_blocks(destination, identity, size, lengths, fetch, *,
                          kind=None, refuse_existing=False, concurrency=BLOCK_CONCURRENCY):
    """Write independently verified blocks; hash ordered bytes once.

    Fresh blocks are hashed from memory, not re-read from disk. On resume only
    retained blocks are read to validate their local checkpoint and rebuild
    the whole-file digest. SHA-256 state is never trusted from a sidecar.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".volume-part")
    checkpoint = destination.with_name(destination.name + ".volume-part.json")
    started = time.perf_counter()
    first_chunk = None
    downloaded = reused = 0
    if refuse_existing and destination.exists():
        raise FileExistsError(destination)
    if not lengths or any(n <= 0 for n in lengths) or sum(lengths) != size:
        raise ValueError("Invalid Volume block layout")
    manifest = {"version": 1, "identity": identity, "size": size, "lengths": lengths}
    try:
        saved = read_json(checkpoint)
    except (OSError, ValueError):
        saved = {}
    if (not isinstance(saved, dict)
            or any(saved.get(k) != v for k, v in manifest.items())
            or not isinstance(saved.get("blocks"), dict) or not partial.is_file()):
        checkpoint.unlink(missing_ok=True)
        partial.unlink(missing_ok=True)
        saved = {**manifest, "blocks": {}}
    blocks = saved["blocks"]
    offsets = []
    offset = 0
    for length in lengths:
        offsets.append(offset)
        offset += length
    digest = hashlib.sha256()
    with partial.open("r+b" if partial.exists() else "w+b") as stream:
        stream.truncate(size)

        async def one(index):
            nonlocal downloaded, reused, first_chunk
            key = str(index)
            length, pos = lengths[index], offsets[index]
            previous = blocks.get(key)
            if isinstance(previous, str):
                stream.seek(pos)
                data = stream.read(length)
                if len(data) == length and hashlib.sha256(data).hexdigest() == previous:
                    reused += length
                    return data
                blocks.pop(key, None)
            data = await fetch(index, pos, length)
            if len(data) != length:
                raise ValueError("Invalid Volume block length")
            if first_chunk is None:
                first_chunk = time.perf_counter() - started
            stream.seek(pos)
            stream.write(data)
            stream.flush()
            blocks[key] = hashlib.sha256(data).hexdigest()
            # If the process crashes before bytes reach disk, re-validation
            # rejects that block; no checksum state is assumed durable.
            atomic_json(checkpoint, saved)
            downloaded += length
            return data

        # Bounded groups retain out-of-order successes without unbounded RAM.
        # All siblings settle so a failing block never loses a successful one.
        for start in range(0, len(lengths), concurrency):
            group = await asyncio.gather(
                *(one(i) for i in range(start, min(start + concurrency, len(lengths)))),
                return_exceptions=True,
            )
            for data in group:
                if isinstance(data, BaseException):
                    raise data
                digest.update(data)
        stream.flush()
        os.fsync(stream.fileno())
        if digest.hexdigest() == identity["sha256"] and kind is not None:
            from runtime.utils.media import media_suffix
            stream.seek(0)
            media_suffix(stream.read(32), kind)

    if digest.hexdigest() != identity["sha256"]:
        # A full mismatch invalidates the checkpoint; do not loop on bad data.
        partial.unlink(missing_ok=True)
        checkpoint.unlink(missing_ok=True)
        raise ValueError("Downloaded content checksum mismatch")
    if refuse_existing and destination.exists():
        raise FileExistsError(destination)
    os.replace(partial, destination)
    checkpoint.unlink(missing_ok=True)
    seconds = time.perf_counter() - started
    return {"downloaded_bytes": downloaded, "reused_bytes": reused,
            "artifact_bytes": size, "seconds": seconds,
            "first_chunk_seconds": first_chunk,
            "mib_per_second": downloaded / (1024 * 1024 * seconds) if seconds else 0,
            "verification": "volume_v2_blocks_streaming_sha256"}


async def _download_modal(volume, remote_path, destination, expected_sha256,
                          max_bytes, kind, refuse_existing):
    """Use the SDK event loop/client, including its configured proxy and TLS."""
    from aiohttp import ClientTimeout
    from modal.exception import NotFoundError
    from modal.volume import (BLOCK_SIZE, _expected_block_lengths, _read_block_body,
                              _raise_on_block_response_error)
    from modal._utils.http_utils import ClientSessionRegistry
    from modal_proto import api_pb2

    async def manifest(start=0, length=0):
        request = api_pb2.VolumeGetFile2Request(
            volume_id=volume.object_id, path=remote_path, start=start, len=length,
            client_pads_blocks=True)
        try:
            async with asyncio.timeout(RPC_TIMEOUT):
                return await volume.client._stub.VolumeGetFile2(request)
        except NotFoundError as exc:
            raise FileNotFoundError(remote_path) from exc

    async with asyncio.timeout(RPC_TIMEOUT):
        await volume.hydrate()
    if volume._is_v1:
        return None
    response = await manifest()
    size = response.len
    if response.start != 0 or not 0 < size <= max_bytes:
        raise ValueError("Volume file exceeds declared size or has invalid range")
    urls = list(response.get_urls)
    lengths = _expected_block_lengths(response.start, size, len(urls))
    if len(urls) != (size + BLOCK_SIZE - 1) // BLOCK_SIZE or sum(lengths) != size:
        raise ValueError("Invalid Volume block layout")

    async def fetch(index, pos, length):
        url = urls[index]
        for attempt in range(BLOCK_ATTEMPTS):
            try:
                if attempt:
                    # Renew only the failed block URL; successful blocks stay.
                    retry = await manifest(pos, length)
                    if retry.start != pos or retry.len != length or len(retry.get_urls) != 1:
                        raise ValueError("Invalid Volume retry range")
                    url = retry.get_urls[0]
                async with asyncio.timeout(BLOCK_TIMEOUT):
                    session = ClientSessionRegistry.get_session()
                    async with session.get(url, timeout=ClientTimeout(
                            total=BLOCK_TIMEOUT, sock_connect=15, sock_read=20)) as resp:
                        await _raise_on_block_response_error(resp)
                        body = await _read_block_body(resp, length)
                    return body.ljust(length, b"\0")
            except Exception as exc:
                # Never include signed URLs or response bodies in durable logs.
                if attempt + 1 == BLOCK_ATTEMPTS:
                    raise OSError(f"Volume block {index} failed: {type(exc).__name__}") from None
                await asyncio.sleep(0.5 * (2 ** attempt))

    identity = {"volume": volume.object_id, "path": remote_path, "sha256": expected_sha256}
    return await _recover_blocks(destination, identity, size, lengths, fetch,
                                 kind=kind, refuse_existing=refuse_existing)


def resumable_download(volume, remote_path, destination, *, expected_sha256,
                       max_bytes, kind=None, refuse_existing=False):
    """Return None only when the resumable transport is unavailable pre-flight."""
    try:
        import modal
        from modal.volume import _expected_block_lengths, _read_block_body, _raise_on_block_response_error
        from modal._utils.async_utils import synchronize_api
        from modal_proto import api_pb2
    except ImportError:
        return None
    if not isinstance(volume, modal.Volume):
        return None
    if not {"start", "len", "client_pads_blocks"} <= set(
            api_pb2.VolumeGetFile2Request.DESCRIPTOR.fields_by_name):
        return None
    # Use Modal's synchronizer (not asyncio.run with a foreign gRPC loop).
    # It translates the public Volume handle to its async counterpart.
    global _sync_download
    with _sync_lock:
        if _sync_download is None:
            _sync_download = synchronize_api(_download_modal, target_module=__name__)
    destination = Path(destination)
    with file_lock(destination.parent, filename="." + destination.name + ".volume-download.lock"):
        return _sync_download(volume, remote_path, destination, expected_sha256,
                              max_bytes, kind, refuse_existing)


_sync_download = None
_sync_lock = threading.Lock()
