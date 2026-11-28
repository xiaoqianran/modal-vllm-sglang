import json
from pathlib import Path

from PIL import Image

from runtime.archive.huggingface import public_url
from runtime.utils.files import sha256_file
from tools.build_hf_history_gallery import build, previous_thumbnails


def test_gallery_embeds_preview_and_survives_local_removal(tmp_path):
    root = tmp_path / "outputs"
    file = root / "project" / "shot.png"
    file.parent.mkdir(parents=True)
    Image.new("RGB", (800, 600), "green").save(file)
    digest = sha256_file(file)
    bucket = "example/media"
    remote = "assets/sha256/" + digest[:2] + "/" + digest + ".png"
    row = {"status": "archived", "verification": "sha256", "bucket": bucket,
           "url": public_url(bucket, remote), "path": remote, "kind": "image",
           "stored_bytes": file.stat().st_size, "original_sha256": digest, "stored_sha256": digest}
    receipt = tmp_path / "receipts.json"
    receipt.write_text(json.dumps({"project/shot.png": row, "project/failed.png": {"status": "failed"}}), encoding="utf-8")
    output = root / "hf-gallery.html"
    count, thumbs, _ = build(root, receipt, output)
    assert (count, thumbs) == (1, 1)
    old = previous_thumbnails(output)
    assert old[digest].startswith("data:image/webp;base64,")
    file.unlink()
    assert build(root, receipt, output)[:2] == (1, 1)
    assert previous_thumbnails(output) == old
    assert 'id="pageSize"' in output.read_text(encoding="utf-8")
