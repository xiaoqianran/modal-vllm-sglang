"""Pure gallery validation; no GPU, network calls or image downloads."""
import tempfile
from pathlib import Path
from unittest.mock import patch
import pytest
from tools.build_cinematic_gallery import collect, render_gallery


def fake_items(url=None):
    return [{"id": f"{i:03d}", "title": f"Scene {i}", "en": f"Frame {i}",
             "prompt": "Cinematic 35mm <frame> & lighting", "status": "completed" if url else "pending",
             "url": url, "bytes": 123} for i in range(1, 13)]


def test_gallery_is_single_document_with_twelve_real_cards():
    url = "https://huggingface.co/buckets/seachen/modal-vllm-sglang-assets/resolve/project/001.png"
    html = render_gallery(fake_items(url))
    assert html.count('<article class="frame">') == 12
    assert html.count('<img src=') == 12
    assert html.count('href="' + url + '"') == 12
    assert '&lt;frame&gt; &amp;' in html
    assert "12<i>/12</i>" in html
    assert "Cinematic" in html


def test_missing_images_are_not_invented():
    html = render_gallery(fake_items())
    assert html.count('<img src=') == 0
    assert html.count("AWAITING FRAME") == 12


def test_gallery_rejects_untrusted_urls():
    with tempfile.TemporaryDirectory() as tmp, patch("tools.build_cinematic_gallery.read_json", side_effect=[{}, {}]), patch(
        "tools.build_cinematic_gallery.build_snapshot"
    ) as snapshot:
        row = {"item": "001", "step": "image", "status": "completed",
               "prompt": "frame", "asset": {"url": "https://example.org/fake.jpg"}}
        snapshot.return_value = {"jobs": [row]}
        with pytest.raises(ValueError, match="HF Bucket"):
            collect(Path(tmp), [{"id": "001", "title": "X", "en": "Y"}])
