import hashlib
import io
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from runtime.archive.video_poster import prepare_video_poster, backfill_video_batch, lookup_video_poster
from runtime.archive.huggingface import public_url
from runtime.archive.config import DEFAULT_ASSET_BUCKET


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),reason="FFmpeg not installed")
def test_extracts_bounded_video_poster(tmp_path):
    source=tmp_path/"sample.mp4"
    subprocess.run(["ffmpeg","-nostdin","-v","error","-y","-f","lavfi","-i",
                    "color=c=blue:s=320x180:d=1.2","-c:v","libx264","-pix_fmt","yuv420p",
                    str(source)],check=True,capture_output=True,timeout=30)
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    file,row=prepare_video_poster(source,tmp_path)
    with Image.open(file) as image:
        assert image.size == tuple(row["dimensions"])
    assert row["dimensions"]==[640,360]
    assert row["bytes"]<=512*1024
    assert hashlib.sha256(file.read_bytes()).hexdigest()==row["sha256"]
    assert digest


@pytest.mark.parametrize("problem,expected", [
    (subprocess.CalledProcessError(1, "ffmpeg", stderr=b"Invalid data found"), "input"),
    (subprocess.TimeoutExpired("ffmpeg", 45), "temporary"),
    (OSError("disk unavailable"), "temporary"),
])
def test_extract_failure_classification(tmp_path, problem, expected):
    from runtime.archive.preview import DerivativeInputError
    outcomes = [subprocess.CompletedProcess("ffprobe", 0, stdout="1.0"), problem, problem]
    with patch("subprocess.run", side_effect=outcomes):
        with pytest.raises(DerivativeInputError if expected == "input" else type(problem)):
            prepare_video_poster(tmp_path / "sample.mp4", tmp_path)


def test_video_poster_backfill_verifies_original_and_batches(tmp_path):
    source=tmp_path/"sample.mp4"; source.write_bytes(b"video source")
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    path=f"assets/sha256/{digest[:2]}/{digest}.mp4"
    url=public_url(DEFAULT_ASSET_BUCKET,path)
    row={"bucket":DEFAULT_ASSET_BUCKET,"status":"archived","verification":"sha256",
         "kind":"video","path":path,"url":url,"stored_sha256":digest,"stored_bytes":source.stat().st_size}
    from runtime.archive import video_poster as module
    poster={"sha256":"a"*64,"bytes":100,"dimensions":[640,360],
            "path":"thumbnails/sha256/aa/"+"a"*64+".webp"}
    poster.update(url=public_url(DEFAULT_ASSET_BUCKET,poster["path"]),verification="sha256")
    def publish(entries,indexes=None):
        return {k:(public_url(DEFAULT_ASSET_BUCKET,k),"sha256",False) for k in entries}
    with patch.object(module,"urlopen",side_effect=lambda *a,**kw:io.BytesIO(source.read_bytes())), \
         patch.object(module,"prepare_video_poster",return_value=(source,poster)), \
         patch("runtime.archive.huggingface.HuggingFaceBucket") as factory:
        factory.return_value.publish_many.side_effect=publish
        result=backfill_video_batch(DEFAULT_ASSET_BUCKET,[row])[url]
        assert result["status"]=="poster"
        assert result["poster"]["url"]==poster["url"]
        bad=backfill_video_batch(DEFAULT_ASSET_BUCKET,[{**row,"stored_sha256":"0"*64}])[url]
        assert "error" in bad
