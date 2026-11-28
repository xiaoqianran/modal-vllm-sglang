import json
from pathlib import Path

from PIL import Image

from orchestration.studio import collect, payload_from_html, publish_catalog, render, safe_url
from runtime.archive.huggingface import public_url
from runtime.utils.files import sha256_file


def test_verified_archive_preview_stays_after_local_removal(tmp_path):
    root = tmp_path / "outputs"
    source = root / "001-project" / "001_image.png"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (60, 40), "green").save(source)
    digest = sha256_file(source)
    bucket = "example/public"
    path = f"assets/sha256/{digest[:2]}/{digest}.png"
    row = {"status": "archived", "verification": "sha256", "bucket": bucket,
           "path": path, "url": public_url(bucket, path), "kind": "image",
           "original_sha256": digest, "stored_bytes": source.stat().st_size}
    receipt = root / ".runtime/history-hf-migration/receipts.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({"001-project/001_image.png": row}), encoding="utf-8")
    first = render(root)
    assert first["assets"][0]["thumb"] == row["url"]
    source.unlink()
    again = render(root)
    assert again["assets"][0]["thumb"] == first["assets"][0]["thumb"]
    assert again["assets"][0]["url"] == row["url"]
    assert len(payload_from_html(root / "index.html", "studio-data")["assets"]) == 1


def test_catalog_is_lightweight_and_escapes_untrusted_text(tmp_path):
    snapshot = {"name": "001-project", "status": "running", "status_label": "</script>",
                "jobs": [{"key": "image/001", "item": "001", "step": "image",
                          "asset": {"url": "001-project/001_image.png", "kind": "image"}},
                         {"key": "image/002", "item": "002", "step": "image",
                          "asset": {"url": "javascript:alert(1)"}}]}
    publish_catalog(tmp_path, [snapshot])
    text = (tmp_path / ".runtime/studio-catalog.js").read_text(encoding="utf-8")
    assert "</script>" not in text
    assert "javascript:" not in text
    assert "001_image.png" in text
    assert '"prompt":null' in text
    assert safe_url("../a.png") is None
    assert safe_url("%2e%2e/a.png") is None
    assert safe_url("https://example.com/a.png") is None




def test_feed_backed_task_initial_payload_is_summary_only(tmp_path):
    root = tmp_path / "outputs"
    image = root / "001-project" / "001_image.png"
    image.parent.mkdir(parents=True)
    Image.new("RGB", (60, 40), "green").save(image)
    runtime = tmp_path / ".runtime" / "monitor"
    runtime.mkdir(parents=True)
    snapshot = {"name":"001-project", "status":"completed", "status_label":"已完成",
        "counts":{"completed":1}, "total":1, "downloaded":1, "updated_at":1,
        "elapsed_ms":10, "started_at":1, "finished_at":2,
        "jobs":[{"key":"image/001","item":"001","step":"image",
                 "prompt":"A unique searchable prompt", "asset":{"url":"001-project/001_image.png",
                 "kind":"image","filename":"001_image.png","bytes":image.stat().st_size,
                 "dimensions":[60,40]}}],
        "shots":[{"item":"001","keys":["image/001"]}],
        "models":[{"name":"test"}], "stages":[{"stage":"generate"}]}
    (runtime / "001-project.js").write_text(
        "window.updateTaskMonitor(" + json.dumps(snapshot,ensure_ascii=False) + ");",
        encoding="utf-8")
    data=collect(root,thumbs=False)
    task=data["tasks"][0]
    assert task["snapshot"]["name"]=="001-project"
    assert "jobs" not in task["snapshot"] and "shots" not in task["snapshot"]
    assert task["feed"]=="../.runtime/monitor/001-project.js"
    assert data["assets"][0]["prompt"]=="A unique searchable prompt"
    assert data["assets"][0]["thumb"]=="001-project/001_image.png"
    assert not data["assets"][0]["thumb"].startswith("data:")



def test_cloud_task_feed_never_keeps_local_thumbnail_path(tmp_path):
    root=tmp_path/"outputs"
    bucket="example/public"
    digest="a"*64
    remote_path=f"assets/sha256/aa/{digest}.png"
    url=public_url(bucket,remote_path)
    receipt={"status":"archived","verification":"sha256","bucket":bucket,
             "path":remote_path,"url":url,"kind":"image","original_sha256":"b"*64,
             "original_bytes":1000,"stored_sha256":digest,"stored_bytes":900}
    receipts=root/".runtime/history-hf-migration/receipts.json"
    receipts.parent.mkdir(parents=True)
    receipts.write_text(json.dumps({"001-project/001_image.png":receipt}),encoding="utf-8")
    runtime=tmp_path/".runtime/monitor"
    runtime.mkdir(parents=True)
    snapshot={"name":"001-project","status":"completed","jobs":[{"key":"image/001","item":"001",
        "step":"image","asset":{"url":url,"local_url":"001-project/001_image.png",
        "thumb":"001-project/old-thumb.webp","kind":"image","filename":"001_image.png","bytes":900}}]}
    (runtime/"001-project.js").write_text("window.updateTaskMonitor("+json.dumps(snapshot)+");",encoding="utf-8")
    data=collect(root,thumbs=False)
    assert data["assets"][0]["thumb"]==url



def test_cloud_feed_keeps_verified_same_bucket_preview(tmp_path):
    root = tmp_path / "outputs"
    runtime = tmp_path / ".runtime/monitor"
    runtime.mkdir(parents=True)
    bucket = "example/public"
    digest = "a" * 64
    preview_digest = "b" * 64
    original = public_url(bucket, f"assets/sha256/aa/{digest}.png")
    preview_path = f"previews/sha256/bb/{preview_digest}.webp"
    preview = public_url(bucket, preview_path)
    candidates = [preview, public_url("other/public", preview_path),
                  "001-project/old-thumb.webp", "https://example.com/preview.webp",
                  public_url(bucket, f"previews/sha256/cc/{preview_digest}.webp")]
    snapshot = {"name": "001-project", "status": "completed", "jobs": [
        {"key": f"image/{i:03}", "item": f"{i:03}", "step": "image",
         "asset": {"url": original, "thumb": candidate, "kind": "image", "filename": "image.png"}}
        for i, candidate in enumerate(candidates)]}
    (runtime / "001-project.js").write_text(
        "window.updateTaskMonitor(" + json.dumps(snapshot) + ");", encoding="utf-8")
    data = collect(root, thumbs=False)
    assert len(data["assets"]) == len(candidates)
    assert data["assets"][0]["thumb"] == preview
    assert all(a["thumb"] == original for a in data["assets"][1:])
    assert all(a["url"] == original for a in data["assets"])
