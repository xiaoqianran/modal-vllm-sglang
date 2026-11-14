"""Header checks shared by inference clients and artifact stores."""
from pathlib import Path


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
