"""Single-request frame staging and safe HTTP forwarding to SGLang."""
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

UPSTREAM = "http://127.0.0.1:30010"
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
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return blob, ".png"
    if blob.startswith(b"\xff\xd8\xff"):
        return blob, ".jpg"
    if blob.startswith(b"RIFF") and blob[8:12] == b"WEBP":
        return blob, ".webp"
    raise ValueError("Only PNG, JPEG and WebP frames are accepted")


def prepare_request(payload):
    """Convert two inline frames to local keyframes in the same container."""
    if not isinstance(payload, dict):
        raise ValueError("Request must be a JSON object")
    data = dict(payload)
    first = data.pop("first_frame_base64", None)
    last = data.pop("last_frame_base64", None)
    task = data.get("task")
    if task not in ("t2va", "fl2va"):
        raise ValueError("Supported task values are t2va and fl2va")
    if data.get("num_inference_steps") != 6:
        raise ValueError("Pika Flex requires num_inference_steps=6")
    if "flow_shift" in data or "audio_flow_shift" in data:
        raise ValueError("Pika Flex controls its own trained sampling schedule")
    if task == "t2va":
        if first or last or data.get("conditions"):
            raise ValueError("T2VA does not accept frames or conditions")
        data["conditions"] = []
        return data
    if not first or not last:
        raise ValueError("FL2VA requires first_frame_base64 and last_frame_base64")
    if data.get("conditions"):
        raise ValueError("Do not manually supply conditions with inline frames")
    # The files are created in the SAME container handling the inference.
    FRAME_DIR.mkdir(parents=True, exist_ok=True)
    conditions = []
    for raw, index in ((first, 0), (last, -1)):
        blob, ext = _decode_frame(raw)
        dest = FRAME_DIR / (uuid.uuid4().hex + ext)
        dest.write_bytes(blob)
        conditions.append({
            "type": "image", "uri": dest.as_uri(),
            "role": "keyframe", "frame_index": index,
        })
    data["conditions"] = conditions
    return data


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _fail(self, code, message):
        body = json.dumps({"error": str(message)}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forward(self, method):
        if not (self.path == "/health" or self.path == "/v1/models"
                or self.path == "/v1/videos"
                or self.path.startswith("/v1/videos/")):
            return self._fail(404, "Unknown endpoint")
        body = None
        if method == "POST":
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= MAX_REQUEST:
                return self._fail(413, "Request too large or empty")
            body = self.rfile.read(length)
            if self.path != "/v1/videos":
                return self._fail(405, "POST supported only for /v1/videos")
            try:
                payload = prepare_request(json.loads(body))
                body = json.dumps(payload).encode("utf-8")
            except (ValueError, json.JSONDecodeError) as exc:
                return self._fail(400, exc)
        req = urllib.request.Request(
            UPSTREAM + self.path, data=body, method=method,
            headers={"Content-Type": "application/json"} if body else {},
        )
        try:
            upstream = urllib.request.urlopen(req, timeout=7200)
        except urllib.error.HTTPError as exc:
            upstream = exc
        except (urllib.error.URLError, TimeoutError) as exc:
            return self._fail(503, f"SGLang not ready: {exc}")
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
    # Do not advertise a ready endpoint until model weights have loaded.
    while True:
        try:
            with urllib.request.urlopen(UPSTREAM + "/health", timeout=2) as r:
                if r.status == 200:
                    break
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(3)
    print("SGLang ready; starting Pika frame proxy at port 30011", flush=True)
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
    ThreadingHTTPServer(("0.0.0.0", 30011), Handler).serve_forever()


if __name__ == "__main__":
    main()
