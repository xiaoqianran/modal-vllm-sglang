"""Isolated coordination stress and CPU reuse experiment."""
import time
import json
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import modal
from runtime.delivery import v3

app = modal.App("lab-delivery-coordination-stress")
image = modal.Image.debian_slim(python_version="3.12").pip_install("modal>=1.6.3,<2").add_local_python_source("runtime","orchestration","pipeline")

@app.function(image=image,cpu=.25,memory=256,timeout=60)
def ended_owner():
    return modal.current_function_call_id()

@app.function(image=image,cpu=.25,memory=256,timeout=60)
def live_owner():
    time.sleep(25)

def stress():
    with modal.Dict.ephemeral() as registry:
        with app.run():
            old=ended_owner.spawn()
            owner=old.get()
            deadline=time.monotonic()+30
            while not v3.terminal_hint(modal,owner) and time.monotonic()<deadline:
                time.sleep(.5)
            assert v3.terminal_hint(modal,owner)
            active=live_owner.spawn()
            try:
                for n in (8,16,32):
                    identity=f"{n:064x}"
                    assert v3.reserve_worker(registry,"original",identity,owner=owner)
                    barrier=Barrier(n)
                    def compete(i):
                        barrier.wait()
                        return v3.reserve_worker(registry,"original",identity,owner=f"fc-lab-{i}",ended=lambda c:c==owner and v3.terminal_hint(modal,c))
                    t=time.monotonic()
                    with ThreadPoolExecutor(n) as pool:
                        winners=sum(pool.map(compete,range(n)))
                    print("TAKEOVER",json.dumps({"contenders":n,"winners":winners,"seconds":round(time.monotonic()-t,3)}),flush=True)
                    assert winners==1
                identity="e"*64
                assert v3.reserve_worker(registry,"derivative",identity,owner=active.object_id)
                assert not v3.reserve_worker(registry,"derivative",identity,owner="fc-other",ended=lambda _:False)
            finally:
                active.cancel()
    for count in (1000,10000):
        with modal.Dict.ephemeral() as registry:
            epoch=int(time.time()//v3.EVENT_WINDOW)
            t=time.monotonic()
            rows={("event",epoch-30000-i):True for i in range(count)}
            rows.update({("event",epoch-i):True for i in range(100)})
            items=list(rows.items())
            for i in range(0,len(items),500):
                registry.update(dict(items[i:i+500]))
            print("SEED",count,round(time.monotonic()-t,3),flush=True)
            t=time.monotonic()
            report=v3.collect_coordination(registry)
            report.update(count=count,seconds=round(time.monotonic()-t,3),remaining=registry.len())
            print("GC",json.dumps(report),flush=True)
            assert report["removed"]<=256 and registry.get(("event",epoch)) is True

if __name__=="__main__":
    stress()
