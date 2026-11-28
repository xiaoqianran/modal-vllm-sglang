"""Shared, bounded delivery admission check for CLI and Studio."""
import asyncio


def check_delivery(bucket, app, delivery_volume, source_volume, *, timeout=90, interval=3):
    try:
        asyncio.run(asyncio.wait_for(
            _check(bucket, app, delivery_volume, source_volume, interval), timeout
        ))
    except asyncio.TimeoutError as exc:
        raise ValueError(f'Delivery readiness check timed out after {timeout}s') from exc


async def _check(bucket, app, delivery_volume, source_volume, interval):
    import modal
    from runtime.delivery import v3

    expected = v3.build_id()
    source = metadata = None
    while True:
        # Resolve a fresh handle on every attempt: a rolling deployment may
        # briefly return the previous function version.
        info = await modal.Function.from_name(app, "check_bucket").remote.aio(bucket, v3.PROTOCOL)
        if source is None:
            source = await modal.Volume.from_name(source_volume, version=2).hydrate.aio()
            metadata = await modal.Volume.from_name(delivery_volume, version=2).hydrate.aio()
        if (not info.get("ready") or info.get("protocol") != v3.PROTOCOL
                or info.get("volume_id") != source.object_id
                or info.get("delivery_volume_id") != metadata.object_id):
            raise ValueError("Delivery archive/storage unavailable: readiness, protocol or volume identity mismatch")
        if info.get("build") == expected:
            return
        await asyncio.sleep(interval)
