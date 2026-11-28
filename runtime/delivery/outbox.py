"""Persistent delivery intents and receipts; no GPU or local client dependency."""
from __future__ import annotations
import json
import os
import re
import time
from urllib.error import HTTPError

from runtime.config.storage import volume_key
from runtime.delivery.config import normalize_delivery
from runtime.tasks.task_identity import intent_digest, safe_id
from runtime.tasks.job_client import remote_record, check_record
from runtime.utils.volume_records import put_once

SCHEMA = 1
PROTOCOL = 2
ROOT = "delivery-v2"
APP = "modal-vllm-sglang-delivery-v2"
MAX_BYTES = 64 * 1024 * 1024
MAX_FILES = 8
MAX_ATTEMPTS = 8
CLAIM_SECONDS = 3600

def enabled(plan):
    return plan.get("delivery_protocol") == PROTOCOL

def app_name():
    return os.getenv("DELIVERY_V2_APP_NAME", APP)

def path(identity, leaf):
    if not re.fullmatch(r"[0-9a-f]{64}", identity):
        raise ValueError("Invalid delivery identity")
    return f"{ROOT}/{identity[:2]}/{identity}/{leaf}"

def read(volume, key):
    try:
        data = bytearray()
        for chunk in volume.read_file(volume_key("/" + key)):
            data.extend(chunk)
            if len(data) > 1024 * 1024:
                raise ValueError("Delivery record too large")
    except FileNotFoundError:
        return None
    return json.loads(data)

def immutable(volume, key, value):
    if not put_once(volume, key, value) and read(volume, key) != value:
        raise ValueError("Conflicting immutable delivery record")

def make_intent(job, record, plan):
    from orchestration.model_capabilities import CAPABILITIES
    settings = normalize_delivery(plan.get("delivery"))
    if settings["provider"] == "local":
        return None
    prefix = record["prefix"]
    if not prefix.startswith("model-results/"):
        raise ValueError("Invalid result namespace")
    for part in prefix.split("/"):
        safe_id(part)
    locator = {k: record[k] for k in ("prefix", "id", "intent_hash", "execution_hash")}
    safe_id(locator["id"])
    if any(not re.fullmatch(r"[0-9a-f]{64}", locator[k]) for k in ("intent_hash", "execution_hash")):
        raise ValueError("Invalid delivery generation identity")
    payload = {"schema": SCHEMA, "locator": locator, "project": safe_id(plan["project"]),
               "kind": CAPABILITIES[job["uses"]]["output"], "delivery": settings}
    from runtime.archive.works import work_job, media_path
    work = work_job(plan, job)
    if work is not None:
        media_path(work, ".png" if payload["kind"] == "image" else ".mp4")
        payload["work"] = work
    return {"id": intent_digest(payload), **payload}

def register(volume, intent):
    if intent is not None:
        immutable(volume, path(intent["id"], "intent.json"), intent)
    return intent

def load_intent(volume, identity):
    intent = read(volume, path(identity, "intent.json"))
    return validate_intent(intent, identity)


def validate_intent(intent, identity):
    if not intent or intent.get("id") != identity or intent.get("schema") != SCHEMA or intent_digest(
            {k:v for k,v in intent.items() if k!="id"}) != identity:
        raise ValueError("Invalid delivery intent")
    loc=intent["locator"]
    if not loc["prefix"].startswith("model-results/"):
        raise ValueError("Invalid result namespace")
    for part in loc["prefix"].split("/"):
        safe_id(part)
    safe_id(loc["id"])
    if any(not re.fullmatch(r"[0-9a-f]{64}",loc[k]) for k in ("intent_hash","execution_hash")):
        raise ValueError("Invalid generation identity")
    if intent["kind"] not in {"image","video"} or normalize_delivery(intent["delivery"])!=intent["delivery"]:
        raise ValueError("Invalid delivery policy")
    safe_id(intent["project"])
    if "work" in intent:
        from runtime.archive.works import media_path
        media_path(intent["work"], ".png" if intent["kind"] == "image" else ".mp4")
    return intent

def resolve(volume, intent, modal_api=None, *, ledger=None, terminal_lookup=None):
    loc = intent["locator"]
    result = remote_record(volume, loc["prefix"], loc["id"])
    if result is None:
        return None
    check_record(loc, result)
    if result["status"] == "collecting" and modal_api is not None:
        # No inference retry: only repair an already committed, verified result
        # after positive terminal evidence of its original GPU call.
        from runtime.tasks.task_lifecycle import terminal_call_status
        from runtime.tasks.result_integrity import verify_remote_media
        from runtime.tasks.job_client import recovery_path
        call = read(volume if ledger is None else ledger, path(intent["id"], "generation-call.json"))
        if call is None:
            from orchestration.submissions import SubmissionLedger
            from orchestration.protocol import SUBMISSION_REGISTRY
            ledger = SubmissionLedger(modal_api.Dict.from_name(SUBMISSION_REGISTRY))
            call = ledger.lookup(loc["prefix"],loc["id"],loc["execution_hash"])
        call_id = (call or {}).get("call_id")
        terminal = (terminal_lookup or terminal_call_status)(modal_api, call_id)
        if terminal and verify_remote_media(volume,loc,result):
            marker = {"source_hash":intent_digest(result),"call_id":call_id,"terminal_status":terminal}
            put_once(volume,recovery_path(loc["prefix"],loc["id"]),marker)
            result=remote_record(volume,loc["prefix"],loc["id"])
            check_record(loc,result)
    if result["status"] != "completed":
        return {"terminal": True} if result["status"] in {"failed","cancelled"} else None
    request = {"artifact":{"storage":"volume","type":intent["kind"],"path":loc["prefix"]+"/"+result["file"],
                            "size":result["bytes"],"sha256":result["sha256"]},
               "delivery":intent["delivery"],"project":intent["project"]}
    if "work" in intent:
        request["work"] = intent["work"]
    return request

def validate_receipt(row, request):
    from runtime.archive.huggingface import public_url
    from runtime.archive.works import media_path
    from pathlib import PurePosixPath
    path_value = row.get("path", "")
    digest = row.get("stored_sha256", "")
    if request.get("work"):
        address_ok = path_value == media_path(request["work"], PurePosixPath(path_value).suffix)
    else:
        address_ok = bool(re.fullmatch(r"assets/sha256/[0-9a-f]{2}/[0-9a-f]{64}\.(png|jpg|webp|mp4)", path_value)
                          and path_value.split("/")[-1].split(".")[0] == digest
                          and path_value.split("/")[-2] == digest[:2])
    if (row.get("status")!="archived" or row.get("verification")!="sha256"
            or row.get("source_artifact")!=request["artifact"] or row.get("bucket")!=request["delivery"]["bucket"]
            or row.get("original_sha256")!=request["artifact"]["sha256"]
            or row.get("original_bytes")!=request["artifact"]["size"]
            or row.get("kind")!=request["artifact"]["type"]
            or type(row.get("stored_bytes")) is not int or row["stored_bytes"]<=0
            or not re.fullmatch(r"[0-9a-f]{64}",row.get("stored_sha256",""))
            or not address_ok
            or (row["kind"] == "video") != path_value.endswith(".mp4")
            or row.get("url")!=public_url(row["bucket"],row["path"])):
        raise ValueError("Invalid durable archive receipt")
    return row

def receipt(volume,intent,request):
    row=read(volume,path(intent["id"],"receipt.json"))
    return validate_receipt(row,request) if row is not None else None

def save_receipt(volume, intent, request, row):
    validate_receipt(row,request)
    key = path(intent["id"], "receipt.json")
    # Concurrent publication of the same immutable content may differ in reused flag.
    if not put_once(volume, key, row):
        return receipt(volume, intent, request)
    return receipt(volume, intent, request)

def classify(exc):
    status = getattr(exc, "code", None) or getattr(getattr(exc,"response",None),"status_code",None)
    if status in (401,403):
        return "blocked_auth"
    if status == 429 or isinstance(status,int) and status >= 500:
        return "retry"
    if isinstance(exc, (ValueError, TypeError, KeyError)) or any(
            x in str(exc) for x in ("SHA256 differs","size differs","pixels differ","checksum mismatch")):
        return "blocked_integrity"
    if "HF_TOKEN is required" in str(exc):
        return "blocked_auth"
    if isinstance(exc, (TimeoutError, OSError)) or status in (404,408):
        return "retry"
    return "retry"

def retry_after(exc):
    headers = getattr(exc,"headers",None) or getattr(getattr(exc,"response",None),"headers",{}) or {}
    value=headers.get("Retry-After",0)
    try:
        seconds=int(value)
    except (TypeError,ValueError):
        from email.utils import parsedate_to_datetime
        try: seconds=int(parsedate_to_datetime(value).timestamp()-time.time())+1
        except (TypeError,ValueError,OverflowError): seconds=0
    return min(3600,max(0,seconds))

def attempts(volume, identity):
    """Bounded append-only failures; no shared mutable state."""
    latest = None
    for n in range(MAX_ATTEMPTS):
        row = read(volume, path(identity, f"failure-{n}.json"))
        if row is None:
            return n, latest
        latest = row
    return MAX_ATTEMPTS, latest

def claim(volume,intent,n,call_id, *, lease_seconds=CLAIM_SECONDS):
    if not isinstance(call_id,str) or not call_id.startswith("fc-"):
        raise ValueError("Invalid archive call identity")
    key=path(intent["id"],f"claim-{n}.json")
    row={"call_id":call_id,"expires_at":time.time()+lease_seconds}
    if put_once(volume,key,row):
        return True
    return read(volume,key)["call_id"]==call_id

def claim_active(volume,intent,n,modal_api):
    row=read(volume,path(intent["id"],f"claim-{n}.json"))
    if not row:
        return False
    if modal_api is not None:
        from runtime.tasks.task_lifecycle import terminal_call_status
        if terminal_call_status(modal_api,row["call_id"]):
            return False
    return time.time()<row["expires_at"]

def failure(volume, intent, n, exc, now=None):
    now = time.time() if now is None else now
    detail = exc if isinstance(exc,dict) else {"kind":classify(exc),"error":type(exc).__name__,"retry_after":retry_after(exc)}
    row = {"kind":detail["kind"],"error":detail["error"],"retry_at":now+max(detail.get("retry_after",0),min(900,15*2**n))}
    key=path(intent["id"],f"failure-{n}.json")
    if not put_once(volume,key,row):
        row=read(volume,key)
    if row["kind"]!="retry" or n+1>=MAX_ATTEMPTS:
        put_once(volume,path(intent["id"],"blocked.json"),row)
    return row

def batches(rows):
    """Group by bucket and bytes; singleton large file is permitted."""
    groups = {}
    for intent, request in rows:
        groups.setdefault(request["delivery"]["bucket"],[]).append((intent,request))
    for group in groups.values():
        chunk, size = [], 0
        for item in group:
            n = item[1]["artifact"]["size"]
            if chunk and (len(chunk)>=MAX_FILES or size+n>MAX_BYTES):
                yield chunk
                chunk,size=[],0
            chunk.append(item)
            size+=n
        if chunk:
            yield chunk

def discover(volume, *, derivative=False):
    prefix = volume_key("/"+ROOT)
    records={}
    try:
        for entry in volume.iterdir(prefix, recursive=True):
            name=entry.path.replace("\\","/")
            parts=name.split("/")
            if len(parts)>=2 and re.fullmatch(r"[0-9a-f]{64}",parts[-2]):
                records.setdefault(parts[-2],set()).add(parts[-1])
    except FileNotFoundError:
        return
    for identity, leaves in records.items():
        if "intent.json" not in leaves:
            continue
        if derivative:
            if "receipt.json" in leaves and "derivative.json" not in leaves:
                yield identity
        elif not ({"receipt.json","terminal.json","blocked.json"} & leaves):
            yield identity

def derivative_receipt(volume,intent,row):
    derived=read(volume,path(intent["id"],"derivative.json"))
    if derived is None:
        return {**row,"derivative_status":"pending"}
    return validate_derivative(derived, row)


def validate_derivative(derived, row):
    if not isinstance(derived, dict):
        raise ValueError("Missing derivative receipt")
    if derived.get("source_sha256")!=row["stored_sha256"]:
        raise ValueError("Invalid derivative source")
    if derived.get("status")=="poster":
        if row["kind"] != "video":
            raise ValueError("Poster requires video")
        from runtime.archive.huggingface import public_url
        poster=derived["poster"]
        digest=poster["sha256"]
        from runtime.archive.works import derivative_path
        expected = derivative_path(row.get("path")) or f"thumbnails/sha256/{digest[:2]}/{digest}.webp"
        if (not re.fullmatch(r"[0-9a-f]{64}",digest)
                or poster["verification"]!="sha256"
                or poster["path"]!=expected
                or poster["url"]!=public_url(row["bucket"],poster["path"])):
            raise ValueError("Invalid video poster")
        return {**row,"poster":poster,"derivative_status":"complete"}
    if derived.get("status")=="preview":
        if row["kind"] != "image":
            raise ValueError("Preview requires image")
        from runtime.archive.preview import preview_url
        result={**row,"preview":derived["preview"],"derivative_status":"complete"}
        if not preview_url(result):
            raise ValueError("Invalid derived preview")
        return result
    if derived.get("status") not in {"original", "failed", "unsupported"}:
        raise ValueError("Unknown derivative status")
    return {**row,"derivative_status":derived["status"]}

def status(volume, identity):
    intent=load_intent(volume,identity)
    blocked=read(volume,path(identity,"blocked.json"))
    if blocked and read(volume,path(identity,"receipt.json")) is None:
        return {"state":"blocked","failure":blocked}
    request=resolve(volume,intent)
    if request is None or request.get("terminal"):
        return {"state":"waiting" if request is None else "generation_failed"}
    row=receipt(volume,intent,request)
    if row:
        try:
            enriched=derivative_receipt(volume,intent,row)
        except (ValueError,KeyError,TypeError):
            enriched={**row,"derivative_status":"failed"}
        return {"state":"archived","receipt":enriched}
    n,last=attempts(volume,identity)
    if n>=MAX_ATTEMPTS or last and last["kind"]!="retry":
        return {"state":"blocked","failure":last}
    if last and time.time()<last["retry_at"]:
        return {"state":"retry_wait","failure":last}
    if claim_active(volume,intent,n,None):
        return {"state":"publishing"}
    return {"state":"pending"}

def collect_ready(volume, identities=None, *, limit=128, modal_api=None):
    ready=[]
    for identity in identities if identities is not None else discover(volume):
        # Existing immutable receipts can be skipped without reading generation data.
        if read(volume,path(identity,"receipt.json")) is not None:
            continue
        try:
            intent=load_intent(volume,identity)
        except (ValueError,KeyError,TypeError):
            # An invalid entry must not starve unrelated valid assets.
            put_once(volume,path(identity,"blocked.json"),{"kind":"blocked_integrity"})
            continue
        n,last=attempts(volume,identity)
        if n>=MAX_ATTEMPTS or last and (last["kind"]!="retry" or time.time()<last["retry_at"]):
            continue
        if read(volume,path(identity,f"claim-{n}.json")) is not None:
            if claim_active(volume,intent,n,modal_api):
                continue
            failure(volume,intent,n,RuntimeError("Archive owner terminated or expired"))
            continue
        try:
            request=resolve(volume,intent,modal_api)
        except Exception as exc:
            row=failure(volume,intent,n,exc)
            continue
        if request and request.get("terminal"):
            put_once(volume,path(identity,"terminal.json"),{"state":"generation_failed"})
        elif request:
            ready.append((intent,request))
            if len(ready)>=limit:
                break
    return ready


def build_id():
    from hashlib import sha256
    from pathlib import Path
    from runtime.archive.config import archive_source_version
    root=Path(__file__).resolve().parent
    h=sha256(archive_source_version().encode())
    for name in ("outbox.py","originals.py","../tasks/job_client.py","../tasks/result_integrity.py",
                 "../tasks/task_lifecycle.py","../utils/volume_records.py","../config/storage.py"):
        h.update(name.encode())
        h.update((root/name).read_bytes().replace(b"\r\n",b"\n"))
    return h.hexdigest()

def assert_build(protocol=PROTOCOL):
    if protocol!=PROTOCOL:
        raise ValueError("Unsupported durable delivery protocol")
    actual=build_id()
    if actual!=os.environ.get("DELIVERY_V2_BUILD"):
        raise RuntimeError("Durable delivery source version mismatch")
    return actual
