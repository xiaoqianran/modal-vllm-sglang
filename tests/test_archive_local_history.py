import json
from pathlib import Path

from PIL import Image

from tools.archive_local_history import reconcile_task_states
from runtime.utils.files import sha256_file


def test_reconcile_verified_receipt_and_preview_to_completed_task(tmp_path):
    output=tmp_path/"outputs"
    media=output/"001-project"
    media.mkdir(parents=True)
    source=media/"001_image.png"
    Image.new("RGB",(32,24),"green").save(source)
    digest=sha256_file(source)
    task=tmp_path/".runtime/tasks/001-project/001-main"
    task.mkdir(parents=True)
    (task/"layout.json").write_text(json.dumps({"media":str(media)}),encoding="utf-8")
    state={"jobs":{"image/001":{"status":"completed","local_artifact":{
        "type":"image","storage":"local","path":"001_image.png","sha256":digest,
        "size":source.stat().st_size}}}}
    state_path=task/"state.json"
    state_path.write_text(json.dumps(state),encoding="utf-8")
    url="https://huggingface.co/buckets/example/public/resolve/assets/sha256/ab/source.png"
    row={"status":"archived","verification":"sha256","bucket":"example/public",
         "path":"assets/sha256/ab/source.png","url":url,"kind":"image",
         "original_sha256":digest,"original_bytes":source.stat().st_size}
    preview={"source_sha256":"stored-digest","status":"preview",
             "preview":{"url":"https://huggingface.co/buckets/example/public/resolve/previews/sha256/ab/thumb.webp",
                        "sha256":"a"*64,"verification":"sha256","path":"previews/sha256/ab/"+"a"*64+".webp",
                        "bytes":100,"dimensions":[32,24],"quality":92,"psnr":42.0}}
    index=output/".runtime/preview-index.json"
    index.parent.mkdir(parents=True)
    index.write_text(json.dumps({url:preview}),encoding="utf-8")
    # The preview index is keyed to the stored digest, not the original digest.
    row["stored_sha256"]="stored-digest"
    assert reconcile_task_states(tmp_path,{ "001-project/001_image.png":row })==1
    updated=json.loads(state_path.read_text(encoding="utf-8"))
    assert updated["jobs"]["image/001"]["cloud_archive"]["status"]=="archived"
    assert updated["jobs"]["image/001"]["cloud_archive"]["preview"]["url"]==preview["preview"]["url"]
    assert updated["jobs"]["image/001"]["local_artifact"]["sha256"]==digest


def test_reconcile_rejects_mismatched_original_hash(tmp_path):
    output=tmp_path/"outputs"; media=output/"001-project"; media.mkdir(parents=True)
    source=media/"001_image.png"; Image.new("RGB",(16,16),"blue").save(source)
    task=tmp_path/".runtime/tasks/001-project/001-main"; task.mkdir(parents=True)
    (task/"layout.json").write_text(json.dumps({"media":str(media)}),encoding="utf-8")
    state_path=task/"state.json"
    state_path.write_text(json.dumps({"jobs":{"image/001":{"status":"completed","local_artifact":{
        "type":"image","storage":"local","path":"001_image.png","sha256":"0"*64,"size":source.stat().st_size}}}}),encoding="utf-8")
    row={"status":"archived","verification":"sha256","original_sha256":"1"*64,
         "original_bytes":source.stat().st_size}
    assert reconcile_task_states(tmp_path,{"001-project/001_image.png":row})==0
    assert "cloud_archive" not in json.loads(state_path.read_text(encoding="utf-8"))["jobs"]["image/001"]
