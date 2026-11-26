from pathlib import Path
import hashlib
from PIL import Image
from orchestration.gallery_derivatives import apply_derivatives, derivative_url, POLICY
from tools.build_studio_derivatives import encode, local_source


def test_thumb_keeps_original_and_preview(tmp_path):
    source=tmp_path/'image.png'; Image.new('RGB',(1280,720),'blue').save(source)
    before=hashlib.sha256(source.read_bytes()).hexdigest()
    payload,dims=encode(source,'image',tmp_path)
    assert dims==[640,360] and hashlib.sha256(source.read_bytes()).hexdigest()==before
    d=hashlib.sha256(payload).hexdigest(); f=tmp_path/'.runtime/gallery-thumbnails'/(d+'.webp'); f.parent.mkdir(parents=True);f.write_bytes(payload)
    url=f'https://huggingface.co/buckets/example/assets/resolve/assets/sha256/{before[:2]}/{before}.png'
    asset={'url':url,'kind':'image','bytes':source.stat().st_size,'thumb':'full-resolution-preview'}
    row={'source_sha256':before,'source_bytes':asset['bytes'],'kind':'image','policy':POLICY,'verification':'sha256','sha256':d,'bytes':len(payload),'path':f'thumbnails/sha256/{d[:2]}/{d}.webp'}
    apply_derivatives([asset],{url:row},tmp_path)
    assert asset['url']==url and asset['thumb']=='full-resolution-preview'
    assert 'gallery_thumb' not in asset
    assert derivative_url(asset,{url:{**row,'source_bytes':1}},tmp_path)==''
    assert derivative_url(asset,{url:{**row,'policy':'old'}},tmp_path)==''


def test_video_poster_does_not_change_playback(tmp_path):
    d='a'*64; t='b'*64; url=f'https://huggingface.co/buckets/example/assets/resolve/assets/sha256/aa/{d}.mp4'; path=f'thumbnails/sha256/bb/{t}.webp'
    asset={'url':url,'kind':'video','bytes':1234,'thumb':'old-poster'}
    row={'source_sha256':d,'source_bytes':1234,'kind':'video','policy':POLICY,'verification':'sha256','sha256':t,'bytes':100,'path':path,'url':f'https://huggingface.co/buckets/example/assets/resolve/{path}'}
    apply_derivatives([asset],{url:row},tmp_path)
    assert asset['url']==url and asset['thumb']==row['url'] and asset['preview']==''
    assert not derivative_url(asset,{url:{**row,'url':'https://evil.example'}},tmp_path)


def test_local_source_rejects_wrong_bytes_and_traversal(tmp_path):
    source=tmp_path/'a.png';source.write_bytes(b'original');d=hashlib.sha256(b'original').hexdigest()
    row={'stored_sha256':d,'stored_bytes':8}
    assert local_source(tmp_path,{'id':'a.png'},row)==source
    source.write_bytes(b'wrong!!!');assert local_source(tmp_path,{'id':'a.png'},row) is None
    assert local_source(tmp_path,{'id':'../a.png'},row) is None
