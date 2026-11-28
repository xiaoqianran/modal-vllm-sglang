import io
import hashlib
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from runtime.archive.preview import prepare_preview, preview_url, backfill_batch, verified_preview
from runtime.archive.huggingface import public_url
from runtime.delivery.cloud_cpu import archive_mounted_batch

BUCKET = "seachen/modal-vllm-sglang-assets"

def sample(path):
    image = Image.new("RGB", (768, 432))
    image.putdata([(x // 4, y // 3, (x+y) // 6) for y in range(432) for x in range(768)])
    image.save(path, compress_level=0)
    return path

def receipt(path):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    remote = f"assets/sha256/{digest[:2]}/{digest}.png"
    return {"status":"archived","verification":"sha256","bucket":BUCKET,"kind":"image",
            "path":remote,"url":public_url(BUCKET,remote),"stored_sha256":digest,"stored_bytes":path.stat().st_size}

def publish(entries, **kwargs):
    return {key:(public_url(BUCKET,key),"sha256",False) for key in entries}

def test_full_resolution_and_verified_index(tmp_path):
    path=sample(tmp_path/"source.png")
    row=receipt(path)
    file, data=prepare_preview(path,tmp_path,row["stored_bytes"])
    with Image.open(file) as image:
        assert image.size == (768,432)
    assert data["psnr"] >= 40 and data["bytes"] < row["stored_bytes"]*.8
    data.update(url=public_url(BUCKET,data["path"]),verification="sha256")
    assert preview_url({**row,"preview":data}) == data["url"]
    assert not preview_url({**row,"preview":{**data,"url":"https://example.org"}})
    index={row["url"]:{"source_sha256":row["stored_sha256"],"preview":data}}
    assert verified_preview(row,index)==data["url"]
    assert not verified_preview({**row,"stored_sha256":"0"*64},index)

def test_backfill_integrity(tmp_path):
    path=sample(tmp_path/"source.png")
    row=receipt(path)
    with patch("urllib.request.urlopen", side_effect=lambda *a,**kw:io.BytesIO(path.read_bytes())), patch("runtime.archive.huggingface.HuggingFaceBucket") as factory:
        factory.return_value.publish_many.side_effect=publish
        result=backfill_batch(BUCKET,[row])
        assert result[row["url"]]["status"]=="preview"
        assert verified_preview(row,result)
        bad=backfill_batch(BUCKET,[{**row,"stored_sha256":"0"*64}])
        assert "error" in bad[row["url"]]
        assert factory.return_value.publish_many.call_count==1

def test_batch_preview_failure_preserves_original(tmp_path):
    file=tmp_path/"model-results/moss/shot.png"
    file.parent.mkdir(parents=True)
    sample(file)
    row=receipt(file)
    artifact={"storage":"volume","type":"image","path":"model-results/moss/shot.png","size":row["stored_bytes"],"sha256":row["stored_sha256"]}
    delivery={"provider":"huggingface","bucket":BUCKET,"compression":"none","verify":"sha256","min_saving":.05}
    request={"artifact":artifact,"delivery":delivery,"project":"test"}
    def fail_preview(entries,**kwargs):
        assert len(entries)==2
        return {key:RuntimeError("preview error") if key.startswith("previews/") else (public_url(BUCKET,key),"sha256",False) for key in entries}
    with patch("runtime.archive.huggingface.HuggingFaceBucket") as factory:
        factory.return_value.publish_many.side_effect=fail_preview
        result=archive_mounted_batch(tmp_path,[request])[0]["receipt"]
        assert result["status"]=="archived" and "preview" not in result
        factory.return_value.publish_many.side_effect=publish
        result=archive_mounted_batch(tmp_path,[request])[0]["receipt"]
        assert preview_url(result)
        assert file.read_bytes() and result["original_sha256"]==artifact["sha256"]


def test_persistent_preview_reuse_and_corruption(tmp_path):
    from runtime.archive.preview import lookup_preview, preview_mapping, preview_index_path
    from unittest.mock import Mock
    source=sample(tmp_path/"original.png")
    row=receipt(source)
    _, preview=prepare_preview(source,tmp_path,row["stored_bytes"])
    preview.update(url=public_url(BUCKET,preview["path"]),verification="sha256")
    payload=preview_mapping(row["stored_sha256"],row["stored_bytes"],preview)
    provider=Mock(bucket=BUCKET)
    assert preview_index_path(row["stored_sha256"]).endswith(row["stored_sha256"]+".json")
    with patch("urllib.request.urlopen",side_effect=lambda *a,**kw:io.BytesIO(payload)):
        assert lookup_preview(provider,row["stored_sha256"],row["stored_bytes"])==preview
        provider.verify_digest.assert_called_once_with(preview["path"],preview["bytes"],preview["sha256"])
        provider.verify_digest.side_effect=RuntimeError("corrupt")
        assert lookup_preview(provider,row["stored_sha256"],row["stored_bytes"]) is None
    with patch("runtime.archive.preview.lookup_preview",return_value=preview), patch("urllib.request.urlopen",side_effect=AssertionError("no original fetch")), patch("runtime.archive.huggingface.HuggingFaceBucket") as factory:
        result=backfill_batch(BUCKET,[row])
        assert result[row["url"]]["preview"]==preview
        factory.return_value.publish_many.assert_not_called()



def test_batch_transport_failure_retries_original_without_preview(tmp_path):
    file=tmp_path/"model-results/moss/retry.png"
    file.parent.mkdir(parents=True)
    sample(file)
    digest=hashlib.sha256(file.read_bytes()).hexdigest()
    artifact={"storage":"volume","type":"image","path":"model-results/moss/retry.png",
              "size":file.stat().st_size,"sha256":digest}
    delivery={"provider":"huggingface","bucket":BUCKET,"compression":"none","verify":"sha256","min_saving":.05}
    request={"artifact":artifact,"delivery":delivery,"project":"retry"}
    original_path=f"assets/sha256/{digest[:2]}/{digest}.png"
    def publish_batch(entries,**kwargs):
        if len(factory.return_value.publish_many.call_args_list)==1:
            raise RuntimeError("transient batch transport error")
        assert list(entries)==[original_path]
        return {original_path:(public_url(BUCKET,original_path),"sha256",False)}
    with patch("runtime.archive.huggingface.HuggingFaceBucket") as factory:
        factory.return_value.publish_many.side_effect=publish_batch
        result=archive_mounted_batch(tmp_path,[request])[0]["receipt"]
        assert result["status"]=="archived"
        assert result["url"]==public_url(BUCKET,original_path)
        assert "preview" not in result
        assert factory.return_value.publish_many.call_count==2
        assert file.is_file()
