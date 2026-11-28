"""Shared, bounded delivery admission check for CLI and Studio."""
import time


def check_delivery(bucket, app, delivery_volume, source_volume, *, timeout=90, interval=3):
    import modal
    from runtime.delivery import v3

    expected = v3.build_id()
    deadline = time.monotonic() + timeout
    source = metadata = None
    while True:
        # Resolve a fresh handle on every attempt: a rolling deployment may
        # briefly return the previous function version.
        info = modal.Function.from_name(app, "check_bucket").remote(bucket, v3.PROTOCOL)
        if source is None:
            source = modal.Volume.from_name(source_volume, version=2).hydrate()
            metadata = modal.Volume.from_name(delivery_volume, version=2).hydrate()
        if (not info.get("ready") or info.get("protocol") != v3.PROTOCOL
                or info.get("volume_id") != source.object_id
                or info.get("delivery_volume_id") != metadata.object_id):
            raise ValueError("Delivery archive/storage unavailable: readiness, protocol or volume identity mismatch")
        if info.get("build") == expected:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError(f"Delivery build mismatch after rollout wait: expected={expected}, actual={info.get('build')}; deploy current delivery")
        time.sleep(min(interval, remaining))
