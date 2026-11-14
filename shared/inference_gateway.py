"""Shared image/video admission gateway, with optional keyframe staging."""
import base64
import binascii
import json
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from shared.media import image_format
from shared.model_spec import operations, operation_spec, parameters
from shared.files import read_json

SPEC = None
SG_PORT, PROXY_PORT = 30010, 30011

UPSTREAM = f"http://127.0.0.1:{SG_PORT}"
FRAME_DIR = Path("/tmp/pika-frames")
MAX_REQUEST = 64 * 1024 * 1024
MAX_FRAME = 20 * 1024 * 1024
FRAME_TTL_SECONDS = 7200


def _decode_frame(value):
    if not isinstance(value, str):
        raise ValueError("Frame must be a base64 string")
    data = value.split(",", 1)[-1] if value.startswith("data:image/") else value
    try:
        blob = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Invalid frame base64") from exc
    if not blob or len(blob) > MAX_FRAME:
        raise ValueError("Frame is empty or larger than 20 MiB")
    return blob, image_format(blob)[0]


def stage_frames(payload):
    """Convert bounded inline images to local files for keyframe APIs."""
    data = dict(payload)
    first, last = data.pop("first_frame_base64", None), data.pop("last_frame_base64", None)
    if first is None and last is None:
        return data
    if first is None or last is None or data.get("conditions"):
        raise ValueError("Provide both inline frames and no manual conditions")
    decoded = [_decode_frame(raw) for raw in (first, last)]
    FRAME_DIR.mkdir(parents=True, exist_ok=True)
    conditions = []
    for (blob, suffix), index in zip(decoded, (0, -1)):
        destination = FRAME_DIR / (uuid.uuid4().hex + suffix)
        destination.write_bytes(blob)
        conditions.append({"type": "image", "uri": destination.resolve().as_uri(),
                           "role": "keyframe", "frame_index": index})
    data["conditions"] = conditions
    return data


def prepare_request(payload, spec):
    if not isinstance(payload, dict):
        raise ValueError("Request must be a JSON object")
    candidates = [name for name in operations(spec)
                  if operation_spec(spec, name)["defaults"].get("task") == payload.get("task")]
    if len(candidates) != 1:
        raise ValueError("Unknown video task")
    op = operation_spec(spec, candidates[0])
    allowed = set(parameters(op, {})) | {"prompt", "seed", "first_frame_base64", "last_frame_base64"}
    if payload.keys() - allowed:
        raise ValueError("Unsupported video request fields")
    if not isinstance(payload.get("prompt"), str) or not payload["prompt"].strip():
        raise ValueError("Nonempty prompt required")
    if type(payload.get("seed")) is not int or not 0 <= payload["seed"] < 2**32:
        raise ValueError("uint32 seed required")
    canonical = parameters(op, {key: payload[key] for key in op["options"] if key in payload})
    for key, value in canonical.items():
        if key not in op["options"] and key in payload and payload[key] != value:
            raise ValueError(f"Fixed parameter cannot be overridden: {key}")
    for field in ("first_frame", "last_frame"):
        present = field + "_base64" in payload
        if present != (field in op["inputs"]):
            raise ValueError("Frames do not match this operation")
    canonical.update({key: value for key, value in payload.items() if key in {"prompt", "seed", "first_frame_base64", "last_frame_base64"}})
    return stage_frames(canonical)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _fail(self, code, message, *, rejected=False):
        body = json.dumps({"error": str(message)}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if rejected:
            self.send_header("X-Generation-Rejected", "1")
        self.end_headers()
        self.wfile.write(body)

    def _forward(self, method):
        if not (self.path == "/health" or self.path == "/v1/models"
                or self.path == "/v1/images/generations"
                or self.path == "/v1/videos"
                or self.path.startswith("/v1/videos/")):
            return self._fail(404, "Unknown endpoint")
        body = None
        if method == "POST":
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                return self._fail(400, "Invalid Content-Length")
            if not 0 < length <= MAX_REQUEST:
                return self._fail(413, "Request too large or empty")
            body = self.rfile.read(length)
            endpoint = "/v1/videos" if SPEC["kind"] == "video" else "/v1/images/generations"
            if self.path != endpoint:
                return self._fail(405, "Unsupported generation endpoint")
            try:
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise ValueError("Request must be a JSON object")
                if SPEC.get("frame_transport") == "keyframes":
                    payload = prepare_request(payload, SPEC)
                body = json.dumps(payload).encode("utf-8")
            except (ValueError, json.JSONDecodeError) as exc:
                return self._fail(400, exc)
        req = urllib.request.Request(
            UPSTREAM + self.path, data=body, method=method,
            headers={"Content-Type": "application/json"} if body else {},
        )
        try:
            try:
                upstream = urllib.request.urlopen(req, timeout=SPEC.get("timeout", 7200))
            except urllib.error.HTTPError as exc:
                upstream = exc
            if method == "POST":
                with upstream:
                    response = upstream.read()
                    code = upstream.status
                    content_type = upstream.headers.get("Content-Type", "application/json")
                # Read the complete engine response before replying to the caller.
                # Reject malformed success responses without inventing a task ID.
                if 200 <= code < 300:
                    if SPEC["kind"] == "video":
                        result = json.loads(response)
                        value = result.get("id") if isinstance(result, dict) else None
                        if type(value) not in (str, int) or not str(value).strip():
                            raise ValueError("Video submission response has no id")
        except (OSError, ValueError, urllib.error.URLError):
            return self._fail(503, "Engine response unavailable; generation acceptance may be unknown")
        if method == "POST":
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            return
        with upstream:
            self.send_response(upstream.status)
            for key in ("Content-Type", "Content-Length"):
                value = upstream.headers.get(key)
                if value:
                    self.send_header(key, value)
            # Use connection-close framing if the upstream length is unknown.
            self.send_header("Connection", "close")
            self.end_headers()
            shutil.copyfileobj(upstream, self.wfile, length=1024 * 1024)
            self.close_connection = True

    def do_GET(self):
        self._forward("GET")

    def do_POST(self):
        self._forward("POST")


def main():
    import argparse
    global SPEC, UPSTREAM, SG_PORT, PROXY_PORT
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    SPEC = read_json(args.config)
    SG_PORT = SPEC.get("port", 8000)
    PROXY_PORT = SPEC.get("proxy_port", SG_PORT + 1)
    UPSTREAM = f"http://127.0.0.1:{SG_PORT}"
    # Do not advertise a ready endpoint until model weights have loaded.
    while True:
        try:
            with urllib.request.urlopen(UPSTREAM + "/health", timeout=2) as r:
                if r.status == 200:
                    break
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(3)
    print("Engine ready; starting shared generation gateway", flush=True)
    def clean_expired_frames():
        """删除两小时以前的临时帧，避免长时间运行耗尽容器磁盘。"""
        while True:
            time.sleep(600)
            if not FRAME_DIR.exists():
                continue
            for frame in FRAME_DIR.iterdir():
                try:
                    if (frame.is_file() and not frame.is_symlink()
                            and time.time() - frame.stat().st_mtime > FRAME_TTL_SECONDS):
                        frame.unlink()
                except OSError:
                    pass

    threading.Thread(target=clean_expired_frames, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PROXY_PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
