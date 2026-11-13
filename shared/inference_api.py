"""Typed-by-behavior clients for existing vLLM image and SGLang video APIs.

Keep model-specific payloads in their callers. The clients only own the
HTTP protocol, response validation and asynchronous video-job lifecycle.
"""
import base64
import binascii
from urllib.parse import quote

from shared.http import poll_job, request_bytes, request_json


class VLLMImageClient:
    """Base64 image protocol shared by vLLM-Omni and SGLang.

    Historical class name retained for existing callers.
    """

    def __init__(self, endpoint, *, headers=None):
        if not endpoint or not str(endpoint).strip():
            raise ValueError("Image API endpoint cannot be empty")
        self.endpoint = str(endpoint).rstrip("/")
        self.options = {"headers": dict(headers)} if headers else {}

    def generate(self, payload, *, timeout=600):
        """Return (image_bytes, extension), validating the actual image format."""
        body = request_json(
            self.endpoint + "/v1/images/generations", payload, timeout=timeout, **self.options
        )
        try:
            encoded = body["data"][0]["b64_json"]
            if not isinstance(encoded, str) or not encoded:
                raise ValueError("Image response has no b64_json")
            image = base64.b64decode(encoded, validate=True)
        except (KeyError, IndexError, TypeError, binascii.Error) as exc:
            raise ValueError("Invalid image generation response or base64 data") from exc
        if image.startswith(b"\x89PNG\r\n\x1a\n"):
            return image, ".png"
        if image.startswith(b"\xff\xd8\xff"):
            return image, ".jpg"
        raise ValueError("Image response is neither PNG nor JPEG")


class SGLangVideoClient:
    """Async /v1/videos submit -> poll -> content API used by SGLang Diffusion."""

    def __init__(self, endpoint, *, headers=None):
        if not endpoint or not str(endpoint).strip():
            raise ValueError("Video API endpoint cannot be empty")
        self.endpoint = str(endpoint).rstrip("/")
        self.options = {"headers": dict(headers)} if headers else {}

    @staticmethod
    def _job_path(job_id):
        if not isinstance(job_id, (str, int)) or not str(job_id).strip():
            raise ValueError("Invalid video job id")
        return "/v1/videos/" + quote(str(job_id), safe="")

    def submit(self, payload, *, timeout=3600):
        """Submit exactly once; an ambiguous timeout must not duplicate a job."""
        response = request_json(self.endpoint + "/v1/videos", payload, timeout=timeout, **self.options)
        job_id = response.get("id") if isinstance(response, dict) else None
        if not isinstance(job_id, (str, int)) or not str(job_id).strip():
            raise ValueError("Video submission response has no id")
        return str(job_id)

    def status(self, job_id, *, timeout=180, attempts=2):
        return request_json(
            self.endpoint + self._job_path(job_id),
            timeout=timeout, attempts=attempts, **self.options,
        )

    def wait(self, job_id, *, timeout=3600, poll=2, status_timeout=180,
             attempts=2, on_status=None):
        def check():
            state = self.status(job_id, timeout=status_timeout, attempts=attempts)
            if on_status is not None:
                on_status(state)
            return state
        return poll_job(check, timeout=timeout, interval=poll, label=f"Video {job_id}")

    def content(self, job_id, *, timeout=600, attempts=2):
        blob = request_bytes(
            self.endpoint + self._job_path(job_id) + "/content",
            timeout=timeout, attempts=attempts, **self.options,
        )
        if len(blob) < 32 or blob[4:8] != b"ftyp":
            raise ValueError(f"Invalid MP4 response: {len(blob)} bytes")
        return blob

    def generate(self, payload, *, timeout=3600, poll=2, submit_timeout=3600,
                 status_timeout=180, content_timeout=600):
        """Convenience for a single video; expose job id for tracking."""
        job_id = self.submit(payload, timeout=submit_timeout)
        self.wait(job_id, timeout=timeout, poll=poll, status_timeout=status_timeout)
        return job_id, self.content(job_id, timeout=content_timeout)
