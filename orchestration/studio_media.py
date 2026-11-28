"""Bounded-memory HTTP delivery of local Studio media."""
import os
import re

CHUNK_BYTES = 256 * 1024


def byte_range(header, length):
    """Return inclusive (start, end), None for ignored ranges, or raise for 416."""
    if not header or not header.startswith("bytes=") or "," in header:
        return None
    match = re.fullmatch(r"bytes=([0-9]{0,20})-([0-9]{0,20})", header)
    if not match or not any(match.groups()):
        return None
    start, end = match.groups()
    if not length:
        raise ValueError("Empty resource")
    if not start:
        suffix = int(end)
        if not suffix:
            raise ValueError("Empty suffix")
        return max(0, length - suffix), length - 1
    start = int(start)
    end = min(int(end), length - 1) if end else length - 1
    if start >= length or end < start:
        raise ValueError("Unsatisfiable range")
    return start, end


def serve_file(handler, path, content_type):
    """Send the file without allocating its full size; HEAD never sends a body."""
    with path.open("rb") as source:
        length = os.fstat(source.fileno()).st_size
        try:
            # Ignore If-Range unless we can validate its representation identifier.
            interval = byte_range(handler.headers.get("Range"), length) if handler.command == "GET" and not handler.headers.get("If-Range") else None
        except ValueError:
            handler.send_response(416)
            handler.send_header("Content-Range", f"bytes */{length}")
            handler.send_header("Accept-Ranges", "bytes")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return
        start, end = interval if interval is not None else (0, length - 1)
        remaining = end - start + 1
        handler.send_response(206 if interval is not None else 200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(remaining))
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Cache-Control", "no-cache")
        if interval is not None:
            handler.send_header("Content-Range", f"bytes {start}-{end}/{length}")
        handler.end_headers()
        if handler.command == "HEAD":
            return
        source.seek(start)
        try:
            while remaining:
                block = source.read(min(CHUNK_BYTES, remaining))
                if not block:
                    break
                handler.wfile.write(block)
                remaining -= len(block)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Browser seeks commonly cancel the preceding request.
            return
