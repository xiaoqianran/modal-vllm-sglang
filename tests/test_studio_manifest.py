import json
import pytest
from tools import studio_manifest as m
from orchestration import studio


def fixture():
    d='a'*64; p='b'*64
    return {'schema':m.SCHEMA,'bucket':'example/assets','projects':['project'], 'assets':[{'id':'project/1','project':'project','name':'1.png','kind':'image','dimensions':[32,24],'path':f'assets/sha256/aa/{d}.png','sha256':d,'bytes':1000,'preview':{'path':f'previews/sha256/bb/{p}.webp','sha256':p,'bytes':90}}]}


def test_roundtrip_empty_directory(tmp_path,monkeypatch):
    template=tmp_path/'template.html'; template.write_text('<script id="studio-data">__STUDIO_DATA__</script>')
    monkeypatch.setattr(studio,'TEMPLATE',template)
    payload=m.encode(fixture()); target=tmp_path/'restore'
    assert m.decode(payload,m.address(payload))==fixture()
    assert m.restore(payload,target)==1
    row=studio.payload_from_html(target/'index.html','studio-data')['assets'][0]
    assert row['storage']=='cloud' and row['thumb'].endswith('.webp')
    assert 'local_url' not in row
    with pytest.raises(FileExistsError): m.restore(payload,target)


def test_reject_corruption_and_unsafe_paths():
    payload=m.encode(fixture())
    with pytest.raises(ValueError): m.decode(payload+b' ',m.address(payload))
    data=fixture(); data['assets'][0]['path']='../secret.png'
    with pytest.raises(ValueError): m.decode(m.encode(data))
    data=fixture(); data['assets'][0]['preview']['path']='previews/fake.webp'
    with pytest.raises(ValueError): m.decode(m.encode(data))
    data=fixture(); data['assets'].append(data['assets'][0])
    with pytest.raises(ValueError): m.decode(m.encode(data))


def test_no_sensitive_fields():
    data=fixture(); data['assets'][0]['prompt']='private prompt'
    with pytest.raises(ValueError): m.decode(m.encode(data))
