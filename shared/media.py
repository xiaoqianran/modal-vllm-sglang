"""Header checks shared by inference clients and artifact stores."""
import base64
import binascii
from pathlib import Path

MAX_FRAME = 20 * 1024 * 1024


def image_format(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    raise ValueError("Expected PNG, JPEG or WebP content")


def image_valid(path, *, minimum=64, webp=False):
    path = Path(path)
    if not path.is_file() or path.stat().st_size < minimum:
        return False
    try:
        with path.open("rb") as stream:
            suffix, _ = image_format(stream.read(16))
        return webp or suffix != ".webp"
    except ValueError:
        return False


def media_suffix(data, kind):
    if kind == "image":
        return image_format(data)[0]
    if len(data) >= 32 and data[4:8] == b"ftyp":
        return ".mp4"
    raise ValueError("Expected MP4 content")


def decode_frame(value):
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
