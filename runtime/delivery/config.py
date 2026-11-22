"""Frozen output policy. No configuration means unchanged local outputs."""
from __future__ import annotations

from runtime.archive.huggingface import public_url


def normalize_delivery(value):
    if value is None:
        return {"provider": "local"}
    if not isinstance(value, dict):
        raise ValueError("delivery must be an object")
    if set(value) - {"provider", "bucket", "compression", "verify", "min_saving"}:
        raise ValueError("Unknown delivery option")
    provider = value.get("provider", "local")
    if not isinstance(provider, str) or provider not in {"local", "huggingface", "both"}:
        raise ValueError("delivery.provider must be local, huggingface or both")
    if provider == "local":
        if set(value) - {"provider"}:
            raise ValueError("Local delivery does not accept Hugging Face options")
        return {"provider": "local"}
    bucket = value.get("bucket")
    if not isinstance(bucket, str):
        raise ValueError("delivery.bucket is required")
    public_url(bucket, "check")
    compression = value.get("compression", "adaptive_lossless")
    verify = value.get("verify", "sha256")
    min_saving = value.get("min_saving", 0.05)
    if not isinstance(compression, str) or compression not in {"none", "adaptive_lossless"}:
        raise ValueError("Unsupported delivery.compression")
    if not isinstance(verify, str) or verify not in {"size", "sha256"}:
        raise ValueError("Unsupported delivery.verify")
    if type(min_saving) not in {float, int} or not 0 <= min_saving < 1:
        raise ValueError("delivery.min_saving must be between 0 and 1")
    return {"provider": provider, "bucket": bucket,
            "compression": compression, "verify": verify,
            "min_saving": float(min_saving)}


def destinations(plan):
    mode = plan.get("delivery", {}).get("provider", "local")
    return ("local", "huggingface") if mode == "both" else (mode,)
