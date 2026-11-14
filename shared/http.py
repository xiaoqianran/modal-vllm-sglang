"""Minimal HTTP client for model APIs.

Retry *read-only* requests only. Never resubmit a generation POST
automatically because the server may have accepted the first request.
"""
import json
import os
import time
import urllib.error
import urllib.request


def modal_proxy_headers(service):
    """Require Proxy Auth before making a request that could start a GPU."""
    key = os.environ.get(f"{service}_MODAL_KEY")
    secret = os.environ.get(f"{service}_MODAL_SECRET")
    if not key or not secret:
        raise ValueError(f"Set {service}_MODAL_KEY and {service}_MODAL_SECRET")
    return {"Modal-Key": key, "Modal-Secret": secret}


def request_bytes(url, payload=None, *, timeout=120, attempts=1, headers=None):
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    if payload is not None and attempts != 1:
        raise ValueError("POST retry is unsafe: use attempts=1")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = dict(headers or {})
    if data is not None:
        headers.setdefault("Content-Type", "application/json")
    for attempt in range(attempts):
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read(1500).decode("utf-8", errors="replace")
            if exc.code in (400, 401, 403, 404, 413, 422) or attempt == attempts - 1:
                raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == attempts - 1:
                raise
        time.sleep(2 * (attempt + 1))


def request_json(url, payload=None, *, timeout=120, attempts=1, headers=None):
    options = {"headers": headers} if headers else {}
    return json.loads(request_bytes(url, payload, timeout=timeout, attempts=attempts, **options))


def poll_job(fetch_status, *, timeout=3600, interval=2, completed="completed",
             failed=("failed", "cancelled"), label="job"):
    if timeout <= 0 or interval <= 0:
        raise ValueError("timeout and interval must be positive")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = fetch_status()
        state = status.get("status")
        if state == completed:
            return status
        if state in failed:
            raise RuntimeError(f"{label} {state}: {status}")
        time.sleep(min(interval, max(0, deadline - time.monotonic())))
    raise TimeoutError(f"{label} exceeded {timeout}s")
