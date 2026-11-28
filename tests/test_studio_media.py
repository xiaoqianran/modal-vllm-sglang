"""HTTP range and backend image-workflow contracts (no cloud execution)."""
import http.client
from contextlib import closing
import threading
from pathlib import Path

import pytest

from orchestration import studio_server
from orchestration.studio_media import byte_range
from orchestration.studio_workflows import image_workflows


@pytest.fixture
def media_server(tmp_path, monkeypatch):
    payload = bytes(range(256)) * 3000
    (tmp_path / "movie.mp4").write_bytes(payload)
    (tmp_path / "secret.txt").write_text("private")
    monkeypatch.setattr(studio_server, "OUTPUT", tmp_path)
    server = studio_server.StudioHTTPServer(("127.0.0.1", 0), object())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def get(port, path="/movie.mp4", method="GET", **headers):
    with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=5)) as conn:
        conn.request(method, path, headers=headers)
        result = conn.getresponse()
        return result.status, dict(result.getheaders()), result.read()


@pytest.mark.parametrize("header,expected", [("bytes=0-9",(0,9)),("bytes=10-",(10,99)),("bytes=-8",(92,99)),
    ("bytes=90-1000",(90,99)),("bytes=-200",(0,99)),("items=0-2",None),("bytes=0-1,4-5",None),("bytes=abc",None)])
def test_byte_range(header, expected):
    assert byte_range(header, 100) == expected


@pytest.mark.parametrize("header", ["bytes=100-", "bytes=15-10", "bytes=-0"])
def test_unsatisfiable_range(header):
    with pytest.raises(ValueError):
        byte_range(header, 100)


def test_real_http_streaming_and_head(media_server):
    port, payload = media_server
    status, headers, raw = get(port, Range="bytes=250000-520000")
    assert status == 206 and raw == payload[250000:520001]
    assert headers["Content-Range"] == f"bytes 250000-520000/{len(payload)}"
    assert headers["Content-Length"] == str(len(raw))
    assert headers["Accept-Ranges"] == "bytes"
    status, headers, raw = get(port, Range="bytes=-100")
    assert status == 206 and raw == payload[-100:]
    status, headers, raw = get(port, Range="bytes=9999999-")
    assert status == 416 and raw == b"" and headers["Content-Range"] == f"bytes */{len(payload)}"
    status, headers, raw = get(port, method="HEAD")
    assert status == 200 and not raw and headers["Content-Length"] == str(len(payload))
    status, _, raw = get(port, Range="bytes=0-9", **{"If-Range":'"stale"'})
    assert status == 200 and raw == payload
    status, _, raw = get(port)
    assert status == 200 and raw == payload


def test_media_allowlist_preserved(media_server):
    port, _ = media_server
    assert get(port, "/secret.txt")[0] == 404
    assert get(port, "/%2e%2e/movie.mp4")[0] == 404
    assert get(port, "/secret.txt", method="HEAD")[2] == b""


def test_catalog_matches_actual_planner():
    root = studio_server.ROOT
    models = image_workflows(root)
    assert models
    for model in models:
        for mode, workflow in model["workflows"].items():
            canvases = model["canvases"][mode]
            assert canvases
            for tier, ratios in canvases.items():
                for ratio, dims in ratios.items():
                    item = {"id":"001","prompt":"test","seed":42}
                    if mode == "i2i":
                        item["image"] = {"artifact":{"path":"model-results/test.png","sha256":"a"*64,"size":10}}
                    result = studio_server._plan_from_task({"version":1,"name":"099-canvas-test","workflow":workflow,
                        "render":{"aspect_ratio":ratio,"resolution":tier},"delivery":{"provider":"local"},"items":[item]})
                    assert len(result["jobs"]) == 1
                    assert result["jobs"][0]["params"]["size"] == f"{dims[0]}x{dims[1]}"
