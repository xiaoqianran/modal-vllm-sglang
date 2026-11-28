"""Deploy separately from the legacy HF archiver; CPU-only durable delivery."""
import os
import time
import modal

from runtime.config.storage import VOLUME_MOUNT, VOLUME_SUB_PATH, VOLUME_VERSION, runtime_volume_name
from runtime.delivery import outbox

app = modal.App(outbox.app_name())
volume = modal.Volume.from_name(runtime_volume_name(), create_if_missing=False, version=VOLUME_VERSION)
source = volume.with_mount_options(read_only=True, sub_path=VOLUME_SUB_PATH)
secret = modal.Secret.from_name("huggingface-secret", required_keys=["HF_TOKEN"])
preferred = os.getenv("DELIVERY_V2_REGION", "us-east")
if preferred == "auto":
    preferred = ""
schedule_enabled = os.getenv("DELIVERY_V2_SCHEDULE", "1") == "1"
image = (modal.Image.debian_slim(python_version="3.12")
         .uv_pip_install("huggingface-hub>=2.4,<3", "pillow>=13", "python-dotenv>=1",
                         "modal>=1.6.3,<2", index_url="https://pypi.org/simple")
         .env({"DELIVERY_V2_BUILD":outbox.build_id(),
               "MODEL_VOLUME_NAME":runtime_volume_name(),
               "DELIVERY_V2_REGION":preferred,
               "DELIVERY_V2_APP_NAME":outbox.app_name()})
         .add_local_python_source("runtime","orchestration",copy=True))
worker_options = dict(image=image,cpu=1,memory=2048,max_containers=4,timeout=600,
                      scaledown_window=60,volumes={VOLUME_MOUNT:source},secrets=[secret])
scanner_options = dict(image=image,cpu=0.25,memory=512,max_containers=1,timeout=600,
                       scaledown_window=2)

@app.function(image=image,cpu=0.25,memory=512,timeout=60,max_containers=1,secrets=[secret])
def check_bucket(bucket,protocol):
    from huggingface_hub import HfApi
    from runtime.archive.huggingface import public_url
    build=outbox.assert_build(protocol)
    public_url(bucket,"readiness")
    api=HfApi(token=os.environ["HF_TOKEN"])
    if api.whoami().get("name")!=bucket.split("/",1)[0] or getattr(api.bucket_info(bucket),"private",False):
        raise ValueError("HF bucket must be public and owned by the configured secret")
    return {"ready":True,"protocol":outbox.PROTOCOL,"build":build,"volume_name":runtime_volume_name()}

@app.function(**worker_options,retries=modal.Retries(max_retries=2,initial_delay=1,backoff_coefficient=2,max_delay=10))
def archive_batch(identities,protocol=outbox.PROTOCOL):
    from runtime.delivery.originals import publish_originals
    outbox.assert_build(protocol)
    volume.reload()
    if not isinstance(identities,list) or not 1<=len(identities)<=outbox.MAX_FILES or len(set(identities))!=len(identities):
        raise ValueError("Invalid delivery batch")
    todo=[]
    for identity in identities:
        intent=outbox.load_intent(volume,identity)
        request=outbox.resolve(volume,intent,modal)
        if request is None or request.get("terminal") or outbox.receipt(volume,intent,request):
            continue
        n,last=outbox.attempts(volume,identity)
        if n>=outbox.MAX_ATTEMPTS or last and (last["kind"]!="retry" or time.time()<last["retry_at"]):
            continue
        if outbox.claim(volume,intent,n,modal.current_function_call_id()):
            todo.append((intent,request,n))
    if not todo:
        return {"archived":0,"submitted_bytes":0}
    metrics={}
    rows=publish_originals(VOLUME_MOUNT,[request for _,request,_ in todo],metrics=metrics)
    for (intent,request,n),row in zip(todo,rows):
        if "receipt" in row:
            outbox.save_receipt(volume,intent,request,row["receipt"])
        else:
            outbox.failure(volume,intent,n,row)
    print("DURABLE_ARCHIVE",metrics,flush=True)
    return metrics

def schedule(fn, groups, *, prefer=False):
    """At most four outstanding batch calls. Never replace an unconfirmed call."""
    from runtime.tasks.task_lifecycle import terminal_call_status
    outcomes=[]
    for i in range(0,len(groups),4):
        calls=[]
        for group in groups[i:i+4]:
            ids=[intent["id"] for intent,_ in group]
            region=preferred if prefer and all(outbox.attempts(volume,x)[0]==0 and
                outbox.read(volume,outbox.path(x,"region-fallback.json")) is None for x in ids) else ""
            try:
                target=fn.with_options(region=region) if region else fn
                call=target.spawn(ids,outbox.PROTOCOL)
                calls.append((call,group,region))
            except Exception as exc:
                # Acceptance may be ambiguous: a later scan uses the same
                # per-attempt claim, so only one worker may publish.
                if region:
                    for identity in ids:
                        outbox.put_once(volume,outbox.path(identity,"region-fallback.json"),{"reason":type(exc).__name__})
                outcomes.append({"dispatch_error":type(exc).__name__})
        for call,group,region in calls:
            try:
                outcomes.append(call.get(timeout=180))
            except Exception as exc:
                terminal=terminal_call_status(modal,call.object_id)
                if terminal and terminal!="SUCCESS" and region:
                    for intent,_ in group:
                        # Initialisation failure may precede the worker claim.
                        # Retry on the default region next scan, not immediately.
                        outbox.put_once(volume,outbox.path(intent["id"],"region-fallback.json"),{"reason":terminal})
                if prefer and terminal and terminal!="SUCCESS":
                    for intent,_ in group:
                        identity=intent["id"]
                        n,last=outbox.attempts(volume,identity)
                        if (outbox.read(volume,outbox.path(identity,"receipt.json")) is None
                                and n<outbox.MAX_ATTEMPTS
                                and not (last and (last["kind"]!="retry" or time.time()<last["retry_at"]))
                                and outbox.read(volume,outbox.path(identity,f"claim-{n}.json")) is None):
                            outbox.failure(volume,intent,n,RuntimeError("Archive invocation failed before claim"))
                outcomes.append({"call_id":call.object_id,"error":type(exc).__name__,"terminal":terminal})
    return outcomes

scan_schedule={"schedule":modal.Cron("* * * * *")} if schedule_enabled else {}
@app.function(**scanner_options,**scan_schedule)
def scan():
    outbox.assert_build()
    ready=outbox.collect_ready(volume,limit=32,modal_api=modal)
    groups=list(outbox.batches(ready))
    return {"ready":len(ready),"batches":len(groups),"results":schedule(archive_batch,groups,prefer=True)}

# Optional derivatives run in a separate pool and never determine archive success.
derivative_image=image.apt_install("ffmpeg")
@app.function(image=derivative_image,cpu=1,memory=2048,max_containers=2,timeout=600,
              scaledown_window=30,secrets=[secret])
def derive_batch(identities,protocol=outbox.PROTOCOL):
    outbox.assert_build(protocol)
    from runtime.archive.preview import backfill_batch
    if not isinstance(identities,list) or not 1<=len(identities)<=4:
        raise ValueError("Invalid derivative batch")
    grouped={}
    for identity in identities:
        intent=outbox.load_intent(volume,identity)
        request=outbox.resolve(volume,intent)
        if not request or request.get("terminal") or outbox.read(volume,outbox.path(identity,"derivative.json")):
            continue
        row=outbox.receipt(volume,intent,request)
        if row:
            grouped.setdefault((row["bucket"],row["kind"]),[]).append((identity,row))
    for (bucket,kind),items in grouped.items():
        receipts=[row for _,row in items]
        if kind=="image":
            results=backfill_batch(bucket,receipts)
        else:
            try:
                from runtime.archive.video_poster import backfill_video_batch
            except ImportError:
                results={r["url"]:{"status":"unsupported","source_sha256":r["stored_sha256"]} for r in receipts}
            else:
                results=backfill_video_batch(bucket,receipts)
        for identity,row in items:
            result=results.get(row["url"],{"error":"Missing derivative result"})
            if "error" in result:
                # Optional processing is retried a bounded number of times.
                n=next((i for i in range(3) if outbox.read(volume,outbox.path(identity,f"derive-failure-{i}.json")) is None),3)
                if n<3:
                    outbox.put_once(volume,outbox.path(identity,f"derive-failure-{n}.json"),{"error":"Derivative failed"})
                if n<2:
                    continue
                result={"status":"failed","source_sha256":row["stored_sha256"]}
            if result.get("source_sha256")!=row["stored_sha256"]:
                result={"status":"failed","source_sha256":row["stored_sha256"]}
            outbox.put_once(volume,outbox.path(identity,"derivative.json"),result)
    return {"processed":sum(map(len,grouped.values()))}

derivative_schedule={"schedule":modal.Cron("*/2 * * * *")} if schedule_enabled else {}
@app.function(**scanner_options,**derivative_schedule)
def scan_derivatives():
    outbox.assert_build()
    ready=[]
    for identity in outbox.discover(volume,derivative=True):
        intent=outbox.load_intent(volume,identity)
        # Original receipts are already verified; worker rechecks their binding.
        ready.append((intent,None))
        if len(ready)>=16:
            break
    groups=[ready[i:i+4] for i in range(0,len(ready),4)]
    return {"ready":len(ready),"results":schedule(derive_batch,groups)}
