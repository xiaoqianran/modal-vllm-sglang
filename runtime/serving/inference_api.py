"""Typed-by-behavior clients for existing vLLM image and SGLang video APIs.

Keep model-specific payloads in their callers. The clients only own the
HTTP protocol, response validation and asynchronous video-job lifecycle.
"""
import base64
import binascii
import urllib.error
from urllib.parse import quote

from runtime.utils.http import poll_job, request_bytes, request_json


class ImageGenerationClient:
    """Base64 image protocol shared by vLLM-Omni and SGLang.

    Protocol behavior is independent of the inference engine.
    """

    def __init__(self, endpoint, *, headers=None, output_directory=None):
        if not endpoint or not str(endpoint).strip():
            raise ValueError("Image API endpoint cannot be empty")
        self.endpoint = str(endpoint).rstrip("/")
        self.options = {"headers": dict(headers)} if headers else {}
        self.output_directory = output_directory

    def generate(self, payload, *, timeout=600):
        """Return (image_bytes, extension), validating the actual image format."""
        payload = self._transport_payload(payload)
        body = request_json(
            self.endpoint + "/v1/images/generations", payload, timeout=timeout, **self.options
        )
        return self._result(body)

    def edit(self, payload, source, *, timeout=600):
        """One multipart request; never retry ambiguous generation."""
        import httpx
        from runtime.utils.media import image_format, MAX_EDIT_IMAGE
        from runtime.tasks.task_lifecycle import SubmissionRejected
        if not 0 < len(source) <= MAX_EDIT_IMAGE:
            raise ValueError("Invalid edit image size")
        suffix, mime = image_format(source)
        data = self._transport_payload(payload)
        data["prompt"] = "<Picture 1> " + data["prompt"]
        with httpx.Client(timeout=timeout, headers=self.options.get("headers")) as client:
            response = client.post(self.endpoint + "/v1/images/edits", data=data,
                                   files={"image": ("source" + suffix, source, mime)})
            if response.is_error:
                if response.headers.get("X-Generation-Rejected") == "1":
                    raise SubmissionRejected("Gateway did not forward this generation")
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:1500]}")
            return self._result(response.json())

    def _transport_payload(self, payload):
        return {**payload, "response_format": "url"} if self.output_directory else dict(payload)

    def _result(self, body):
        if self.output_directory is None:
            return self._decode(body)
        # Both server and client live in the same container. Never fetch a URL,
        # trust a caller path, or retry a completed inference for transport errors.
        from pathlib import Path
        from runtime.utils.media import image_format, MAX_EDIT_IMAGE
        root = Path(self.output_directory).resolve()
        entry = body["data"][0]
        path = Path(entry["file_path"])
        if not path.is_absolute() or path.is_symlink() or path.resolve().parent != root:
            raise ValueError("Engine output escaped its dedicated directory")
        if not path.is_file():
            raise ValueError("Engine output is not a regular file")
        try:
            with path.open("rb") as stream:
                blob = stream.read(MAX_EDIT_IMAGE + 1)
            if not 0 < len(blob) <= MAX_EDIT_IMAGE:
                raise ValueError("Engine image exceeds output limit")
            return blob, image_format(blob)[0]
        finally:
            path.unlink(missing_ok=True)

    @staticmethod
    def _decode(body):
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
        try:
            return request_json(
                self.endpoint + self._job_path(job_id),
                timeout=timeout, attempts=attempts, **self.options,
            )
        except RuntimeError as exc:
            cause = exc.__cause__
            if isinstance(cause, urllib.error.HTTPError) and cause.code == 404:
                raise RuntimeError(f"Video {job_id} is unavailable on this engine; recovery cannot continue. "
                                   "Inspect persisted results before explicitly creating a new task.") from exc
            raise

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


# Compatibility alias for external callers of the original image API.
VLLMImageClient = ImageGenerationClient
