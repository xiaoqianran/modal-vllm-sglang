"""CPU-only regression coverage for delivery protocol 2."""
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from unittest.mock import Mock

import pytest

from runtime.config.storage import volume_key
from runtime.delivery import outbox
from runtime.delivery.config import normalize_delivery
from runtime.delivery.originals import publish_originals
from runtime.archive.huggingface import public_url
from tests.test_sdk_reuse import Volume

PNG=b"\x89PNG\r\n\x1a\n"+b"test data"*32
SETTINGS=normalize_delivery({"provider":"huggingface","bucket":"test/isolated","compression":"none"})

class Ledger(Volume):
    def iterdir(self,prefix,recursive=True):
        return super().iterdir(prefix.lstrip("/"),recursive=recursive)

def fixture(volume, n=0, state="completed", raw=PNG):
    locator={"prefix":"model-results/moss/001-lab/001-main/t2i-test","id":f"j{n}",
             "intent_hash":"a"*64,"execution_hash":"b"*64}
    plan={"project":"001-lab","delivery":SETTINGS,"delivery_protocol":2}
    intent=outbox.register(volume,outbox.make_intent({"uses":"moss.t2i"},locator,plan))
    result={"id":locator["id"],"file":locator["id"]+".png","bytes":len(raw),
            "sha256":hashlib.sha256(raw).hexdigest(),"status":state,
            "intent_hash":locator["intent_hash"],"execution_hash":locator["execution_hash"]}
    if state:
        volume.files[volume_key("/"+locator["prefix"]+"/"+locator["id"]+".json")]=json.dumps(result).encode()
        volume.files[volume_key("/"+locator["prefix"]+"/"+result["file"])]=raw
    return intent,result

def make_receipt(request):
    a=request["artifact"]; digest=a["sha256"]
    path=f"assets/sha256/{digest[:2]}/{digest}.png"
    return {"source_artifact":a,"status":"archived","verification":"sha256","bucket":SETTINGS["bucket"],
            "original_sha256":digest,"original_bytes":a["size"],"stored_sha256":digest,
            "stored_bytes":a["size"],"kind":"image","path":path,"url":public_url(SETTINGS["bucket"],path)}

def test_outbox_durable_before_media_and_discovery():
    v=Ledger();i,r=fixture(v,state=None)
    assert outbox.status(v,i["id"])["state"]=="waiting"
    assert list(outbox.discover(v))==[i["id"]]
    assert not outbox.collect_ready(v)
    fixture(v)
    assert outbox.collect_ready(v)[0][0]==i
    assert outbox.register(v,i)==i

def test_claim_is_atomic_and_recovery_respects_backoff(monkeypatch):
    v=Ledger();i,_=fixture(v)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(lambda n:outbox.claim(v,i,0,"fc-"+str(n)),range(8)))
    assert sum(results)==1
    assert outbox.status(v,i["id"])["state"]=="publishing"
    assert outbox.collect_ready(v)==[]
    monkeypatch.setattr("runtime.tasks.task_lifecycle.terminal_call_status",lambda *a:"FAILURE")
    assert outbox.collect_ready(v,modal_api=Mock())==[]
    assert outbox.status(v,i["id"])["state"]=="retry_wait"
    monkeypatch.setattr(outbox.time,"time",lambda:10**12)
    assert outbox.collect_ready(v)[0][0]==i
    assert outbox.claim(v,i,1,"fc-new")

@pytest.mark.parametrize("code,kind",[(401,"blocked_auth"),(403,"blocked_auth"),(429,"retry"),(503,"retry")])
def test_error_classification_and_retry_after(code,kind):
    v=Ledger();i,_=fixture(v)
    e=HTTPError("https://example.invalid",code,"failure",{"Retry-After":"120"},None)
    failure=outbox.failure(v,i,0,e,now=100)
    assert failure["kind"]==kind and failure["retry_at"]==220
    if kind.startswith("blocked"):
        assert outbox.status(v,i["id"])["state"]=="blocked"
        assert not list(outbox.discover(v))

def test_receipt_is_immutable_and_derivatives_do_not_block():
    v=Ledger();i,_=fixture(v)
    req=outbox.resolve(v,i);row=make_receipt(req)
    with pytest.raises(ValueError):
        outbox.save_receipt(v,i,req,{**row,"stored_sha256":"c"*64})
    assert outbox.read(v,outbox.path(i["id"],"receipt.json")) is None
    assert outbox.save_receipt(v,i,req,row)==row
    assert outbox.save_receipt(v,i,req,{**row,"reused":True})==row
    assert outbox.status(v,i["id"])["receipt"]["derivative_status"]=="pending"
    assert list(outbox.discover(v,derivative=True))==[i["id"]]
    outbox.immutable(v,outbox.path(i["id"],"derivative.json"),{"status":"failed","source_sha256":row["stored_sha256"]})
    assert outbox.status(v,i["id"])["receipt"]["derivative_status"]=="failed"
    assert not list(outbox.discover(v,derivative=True))
    assert not outbox.collect_ready(v)

def test_collecting_recovery_requires_terminal_and_verified_media(monkeypatch):
    v=Ledger();i,r=fixture(v,state="collecting")
    outbox.immutable(v,outbox.path(i["id"],"generation-call.json"),{"call_id":"fc-old"})
    monkeypatch.setattr("runtime.tasks.task_lifecycle.terminal_call_status",lambda *a:None)
    assert outbox.resolve(v,i,Mock()) is None
    monkeypatch.setattr("runtime.tasks.task_lifecycle.terminal_call_status",lambda *a:"TERMINATED")
    assert outbox.resolve(v,i,Mock())["artifact"]["sha256"]==r["sha256"]
    assert outbox.resolve(v,i)["artifact"]["sha256"]==r["sha256"]

def test_batch_by_bucket_and_bytes():
    rows=[]
    for n in range(17):
        v=Ledger();i,_=fixture(v,n);rows.append((i,outbox.resolve(v,i)))
    assert [len(g) for g in outbox.batches(rows)]==[8,8,1]
    for _,request in rows:
        request["artifact"]["size"]=32*1024*1024
    assert [len(g) for g in outbox.batches(rows)]==[2]*8+[1]

def test_original_only_batch_partial_success(tmp_path):
    v=Ledger();i,_=fixture(v,1);j,_=fixture(v,2,raw=PNG+b"x")
    requests=[outbox.resolve(v,x) for x in (i,j)]
    for req in requests:
        a=req["artifact"];p=tmp_path/a["path"];p.parent.mkdir(parents=True,exist_ok=True)
        p.write_bytes(v.files[volume_key("/"+a["path"])])
    provider=Mock()
    def publish(entries,**kwargs):
        assert len(entries)==2
        return {path: (public_url(SETTINGS["bucket"],path),"sha256",False)
                if n==0 else HTTPError("https://example.invalid",429,"rate",{"Retry-After":"45"},None)
                for n,path in enumerate(entries)}
    provider.publish_many.side_effect=publish
    rows=publish_originals(tmp_path,requests,provider_factory=lambda _:provider)
    assert "receipt" in rows[0] and "preview" not in rows[0]["receipt"]
    assert rows[1]["kind"]=="retry" and rows[1]["retry_after"]==45
    assert provider.publish_many.call_count==1

def test_build_protocol_separate_from_local_version(monkeypatch):
    monkeypatch.setenv("DELIVERY_V2_BUILD",outbox.build_id())
    assert outbox.assert_build(2)==outbox.build_id()
    with pytest.raises(ValueError):
        outbox.assert_build(1)
    monkeypatch.setenv("DELIVERY_V2_BUILD","wrong")
    with pytest.raises(RuntimeError):
        outbox.assert_build(2)

def test_new_task_uses_current_protocol_by_default(tmp_path):
    from orchestration import state
    from pipeline.s01_build_plan import ROOT, build_plan
    import shutil
    shutil.copytree(ROOT / "workflows", tmp_path / "workflows")
    p = tmp_path / "tasks/099-durable.json"
    task = {"version": 1, "name": "099-durable", "workflow": "moss-t2i",
            "items": [{"id": "001", "prompt": "mock"}], "delivery": SETTINGS}
    state.save(p, task)
    plan = build_plan(p, root=tmp_path)
    assert plan["delivery_protocol"] == 3
    assert plan["delivery"]["verify"] == "sha256"
    state.save(p, {**task, "delivery_protocol": 3})
    assert build_plan(p, root=tmp_path) == plan
    for obsolete in (1, 2, True):
        state.save(p, {**task, "delivery_protocol": obsolete})
        with pytest.raises(ValueError, match="Unsupported delivery protocol"):
            build_plan(p, root=tmp_path)
    state.save(p, {**task, "delivery_protocol": 3, "delivery": {"provider": "local"}})
    with pytest.raises(ValueError):
        build_plan(p, root=tmp_path)


def test_obsolete_protocol_cannot_dispatch():
    from orchestration.sdk import Backend
    backend = Backend.__new__(Backend)
    for protocol in (None, 1, 2):
        with pytest.raises(ValueError, match="Obsolete delivery protocol"):
            backend.delivery_store({"delivery_protocol": protocol})
