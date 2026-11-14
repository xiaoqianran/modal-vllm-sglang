"""Durable local receipts for detached Modal calls; never retry an ambiguous spawn."""
import json
from pathlib import Path

from shared.batch import intent_digest
from shared.files import atomic_json, download_volume_file, file_lock, read_json, sha256_file
from shared.model_cache import cache_directory, component_specs
from shared.outputs import record_artifact


def remote_record(volume, prefix, shot):
    try:
        return json.loads(b"".join(volume.read_file(f"/{prefix}/{shot}.json")))
    except FileNotFoundError:
        return None


def check_record(job, record):
    if (record.get("intent_hash") != job["intent_hash"]
            or record.get("execution_hash") != job["execution_hash"]):
        raise ValueError("Task/configuration/cache conflict; refusing to overwrite or resubmit")
    if record.get("status") == "completed":
        if (record.get("id") != job["id"]
                or record.get("file") not in {job["id"] + s for s in (".png", ".jpg", ".webp", ".mp4")}
                or not isinstance(record.get("sha256"), str) or len(record["sha256"]) != 64):
            raise ValueError("Invalid completed result")


def refresh(job, volume, call_factory):
    """Volume survives expired call outputs. A missing file never authorizes a retry."""
    record = remote_record(volume, job["prefix"], job["id"])
    if record:
        check_record(job, record)
        if record.get("status") == "completed":
            job.update(status="completed", result=record)
            job.pop("error", None)
            return
        if record.get("status") in {"failed", "cancelled"}:
            job.update(status="failed", error="Generation failed; inspect remote metadata")
            return
    if job.get("call_id"):
        try:
            result = call_factory(job["call_id"]).get(timeout=0)
        except TimeoutError:
            job["status"] = "running" if record and record.get("status") in {"submitting", "running", "collecting"} else "queued"
            return
        except Exception as exc:
            # Includes expired receipts, transport errors and remote exceptions.
            # None proves it is safe to pay for another generation.
            job.update(status="uncertain", error=f"{type(exc).__name__}: {exc}")
            return
        check_record(job, result)
        if result.get("status") != "completed":
            raise ValueError("Modal returned an incomplete result")
        job.update(status="completed", result=result)
        job.pop("error", None)
    elif job.get("status") != "pending":
        job["status"] = "uncertain"


def collect(job, volume, output):
    record = job["result"]
    check_record(job, record)
    folder = output / job["project"] / job["scene"]
    with file_lock(folder):
        target = folder / record["file"]
        if target.exists():
            if sha256_file(target) != record["sha256"]:
                raise FileExistsError(target)
        else:
            download_volume_file(volume, f"/{job['prefix']}/{record['file']}", target,
                                 expected_sha256=record["sha256"], kind=job["kind"], refuse_existing=True)
        atomic_json(folder / ".runner" / "items" / job["id"] / job["kind"] / "metadata.json", record)
        record_artifact(folder, target, job["kind"], source=f"/{job['prefix']}/{record['file']}")
    job["collected"] = True


def run_jobs(spec, paths, *, root=None, output=None, limit=0, dry_run=False, mode="enqueue"):
    import modal
    from shared.modal_setup import model_volume
    from shared.model_service import ROOT, load_task, prepare
    root = Path(root) if root is not None else ROOT
    output = Path(output) if output is not None else Path("outputs")
    if not output.is_absolute():
        output = root / output
    # Validate every input before connecting or submitting anything.
    selected = []
    identities = {}
    for path in paths:
        project, scene, operation, items = load_task(spec, path, root=root, limit=limit)
        prefix = f"model-results/{spec['service']}/{project}/{scene}/{operation}-{intent_digest(spec)[:20]}"
        for item in items:
            signature = prepare(spec, item)[1]
            key = f"{project}/{scene}/{item['id']}/{spec['kind']}".lower()
            if key in identities and identities[key] != signature:
                raise ValueError(f"Conflicting task files: {key}")
            identities[key] = signature
            selected.append((project, scene, prefix, item, signature))
    if dry_run:
        print(json.dumps({"mode": mode, "items": len(selected), "service": spec["service"]}))
        return []
    volume = model_volume()
    fingerprint = None
    if mode == "enqueue":
        try:
            cache = json.loads(b"".join(volume.read_file(cache_directory(spec).removeprefix("/mnt/modal") + "/model-ready.json")))
        except FileNotFoundError as exc:
            raise RuntimeError("Prepare model on CPU with download_model.py first") from exc
        if cache.get("version") != 1 or cache.get("cache_spec") != intent_digest(component_specs(spec)):
            raise RuntimeError("Prepared cache differs from model.json; prepare it again")
        fingerprint = intent_digest(cache)
    directory = output / ".runner" / "jobs"
    manifest = directory / (spec["service"] + ".json")
    with file_lock(directory):
        jobs = read_json(manifest) if manifest.exists() else {}
        keys = []
        for project, scene, prefix, item, signature in selected:
            key = f"{project}/{scene}/{item['id']}/{spec['kind']}".lower()
            keys.append(key)
            old = jobs.get(key)
            if old and (old["intent_hash"] != signature or (fingerprint and old["cache_fingerprint"] != fingerprint)):
                raise ValueError(f"Task/configuration/cache conflict: {key}")
        model = None
        for project, scene, prefix, item, signature in selected if mode == "enqueue" else []:
            key = f"{project}/{scene}/{item['id']}/{spec['kind']}".lower()
            if key not in jobs:
                job = {"id": item["id"], "project": project, "scene": scene, "prefix": prefix,
                       "kind": spec["kind"], "status": "pending", "intent_hash": signature,
                       "cache_fingerprint": fingerprint,
                       "execution_hash": intent_digest({"intent": signature, "cache": fingerprint})}
                folder = output / project / scene
                # Existing output must be reconciled through committed Volume metadata.
                remote = remote_record(volume, prefix, item["id"])
                if remote:
                    check_record(job, remote)
                    job["status"] = "uncertain"
                elif any((folder / (item["id"] + s)).exists() for s in (".png", ".jpg", ".jpeg", ".webp", ".mp4")):
                    raise FileExistsError(f"Unverified existing output: {key}")
                jobs[key] = job
            job = jobs[key]
            refresh(job, volume, modal.FunctionCall.from_id)
            if job["status"] == "pending":
                if model is None:
                    model = modal.Cls.from_name(spec.get("app_name", "modal-model-" + spec["service"]), spec.get("modal_class", "Model"))()
                job["status"] = "submitting"
                atomic_json(manifest, jobs)  # Must reach disk BEFORE the network request.
                try:
                    call = model.generate_one.spawn(item, prefix, fingerprint, intent_digest(spec))
                    job.update(call_id=call.object_id, status="queued")
                except BaseException:
                    job["status"] = "uncertain"
                    atomic_json(manifest, jobs)
                    raise
            atomic_json(manifest, jobs)
            print(json.dumps({"job": key, **job}, ensure_ascii=False))
        if mode != "enqueue":
            keys = keys if paths else list(jobs)
            for key in dict.fromkeys(keys):
                if key not in jobs:
                    print(json.dumps({"job": key, "status": "not_submitted"}))
                    continue
                job = jobs[key]
                refresh(job, volume, modal.FunctionCall.from_id)
                atomic_json(manifest, jobs)
                if mode == "collect" and job["status"] == "completed":
                    collect(job, volume, output)
                    atomic_json(manifest, jobs)
                print(json.dumps({"job": key, **job}, ensure_ascii=False))
        return jobs
