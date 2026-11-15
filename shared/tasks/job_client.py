"""Durable per-model receipts for detached Modal calls; never retry an ambiguous spawn."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import time
from pathlib import Path

from shared.tasks.batch import intent_digest
from shared.utils.files import atomic_json, download_volume_file, file_lock, read_json, sha256_file
from shared.config.model_cache import cache_directory, component_specs
from shared.utils.outputs import record_artifact
from shared.tasks.task_spec import ROOT, load_task, prepare, task_key, result_prefix, execution_signature

DOWNLOAD_WORKERS = 8


def read_jobs(manifest):
    # Writers use atomic replacement; readers see a complete old or new version.
    return read_json(manifest) if manifest.exists() else {}


def save_job(manifest, key, job):
    """Merge one task under a short local lock; never overwrite other runners."""
    with file_lock(manifest.parent, timeout=10):
        jobs = read_jobs(manifest)
        jobs[key] = job
        atomic_json(manifest, jobs)


def job_lock(manifest, key):
    # Same identity stays exclusive across refresh/spawn; unrelated tasks proceed.
    return file_lock(manifest.parent, filename=f".job-{intent_digest([manifest.name, key])}.lock")


def remote_cache_fingerprint(spec, volume):
    try:
        cache = json.loads(b"".join(volume.read_file(
            cache_directory(spec).removeprefix("/mnt/modal") + "/model-ready.json")))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Remote cache manifest missing for {spec['service']}; "
                           "run download_model.py --migrate-only or prepare it on CPU first") from exc
    if cache.get("version") != 1 or cache.get("cache_spec") != intent_digest(component_specs(spec)):
        raise RuntimeError("Remote cache differs from current model configuration; prepare or migrate it on CPU")
    return intent_digest(cache)


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
    # 同一场景的不同镜头互不阻塞，同一镜头仍独占写入锁。
    with file_lock(folder, filename=f".runner.{job['id']}.{job['kind']}.lock"):
        target = folder / record["file"]
        metrics = {"downloaded_bytes": 0, "reused": True}
        if target.exists():
            if sha256_file(target) != record["sha256"]:
                raise FileExistsError(target)
        else:
            metrics = download_volume_file(volume, f"/{job['prefix']}/{record['file']}", target,
                                           expected_sha256=record["sha256"], kind=job["kind"], refuse_existing=True)
        atomic_json(folder / ".runner" / "items" / job["id"] / job["kind"] / "metadata.json", record)
        record_artifact(folder, target, job["kind"], source=f"/{job['prefix']}/{record['file']}")
    job["collected"] = True
    job["download_metrics"] = metrics
    print(json.dumps({"event": "download_completed", "id": job["id"],
                      "prefix": job["prefix"], **metrics}, ensure_ascii=False))


def parallel_collect(jobs, volume, output):
    """八路下载；主线程收集结果，失败不影响其余镜头完成。"""
    errors = []
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as executor:
        future_jobs = {executor.submit(collect, job, volume, output): job for job in jobs}
        for future in as_completed(future_jobs):
            try:
                future.result()
            except Exception as exc:
                errors.append(exc)
    collection_summary(future_jobs.values(), started, errors=len(errors))
    return errors


def collection_summary(jobs, started, *, errors=0):
    jobs = list(jobs)
    seconds = time.perf_counter() - started
    size = sum(job.get("download_metrics", {}).get("downloaded_bytes", 0) for job in jobs)
    print(json.dumps({"event": "collect_summary", "items": len(jobs), "workers": DOWNLOAD_WORKERS,
                      "errors": errors, "downloaded_bytes": size, "seconds": round(seconds, 3),
                      "mib_per_second": size / (1024 * 1024 * seconds) if seconds else 0}, ensure_ascii=False))


def collect_remote(spec, selected, volume, output):
    """按 Task 身份从 Volume 并发收集图片或视频，不依赖本地登记表。"""
    fingerprint = remote_cache_fingerprint(spec, volume)
    result = {}
    failures = []
    started = time.perf_counter()
    def fetch(entry):
        project, scene, prefix, item, signature = entry
        key = task_key(project, scene, item["id"], spec["kind"])
        job = {"id": item["id"], "project": project, "scene": scene, "prefix": prefix,
               "kind": spec["kind"], "intent_hash": signature,
               "execution_hash": execution_signature(signature, fingerprint)}
        record = remote_record(volume, prefix, item["id"])
        if record is None:
            job["status"] = "not_found"
        else:
            check_record(job, record)
            job["status"] = record.get("status", "unknown")
            if job["status"] == "completed":
                job["result"] = record
                collect(job, volume, output)
        return key, job

    unique = {}
    for project, scene, prefix, item, signature in selected:
        key = task_key(project, scene, item["id"], spec["kind"])
        unique.setdefault(key, (project, scene, prefix, item, signature))
    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as executor:
        futures = {executor.submit(fetch, entry): key for key, entry in unique.items()}
        for future in as_completed(futures):
            key = futures[future]
            try:
                _, job = future.result()
                result[key] = job
                print(json.dumps({"job": key, "status": job["status"],
                                  "collected": job.get("collected", False)}, ensure_ascii=False))
            except Exception as exc:
                failures.append((key, exc))
                print(json.dumps({"job": key, "status": "error", "error": str(exc)}, ensure_ascii=False))
    collection_summary([result.get(key, {}) for key in unique], started, errors=len(failures))
    if failures:
        raise failures[0][1]
    return result


def run_jobs(spec, paths, *, root=None, output=None, limit=0, dry_run=False, mode="enqueue"):
    import modal
    from shared.serving.modal_setup import model_volume
    root = Path(root) if root is not None else ROOT
    output = Path(output) if output is not None else Path("outputs")
    if not output.is_absolute():
        output = root / output
    # Validate every input before connecting or submitting anything.
    selected = []
    identities = {}
    for path in paths:
        project, scene, operation, items = load_task(spec, path, root=root, limit=limit)
        prefix = result_prefix(spec, project, scene, operation)
        for item in items:
            signature = prepare(spec, item)[1]
            key = task_key(project, scene, item["id"], spec["kind"])
            if key in identities and identities[key] != signature:
                raise ValueError(f"Conflicting task files: {key}")
            identities[key] = signature
            selected.append((project, scene, prefix, item, signature))
    if dry_run:
        print(json.dumps({"mode": mode, "items": len(selected), "service": spec["service"]}))
        return []
    volume = model_volume()
    if mode == "collect_remote":
        if not paths:
            raise ValueError("--collect --remote requires at least one --task")
        return collect_remote(spec, selected, volume, output)
    fingerprint = None
    if mode == "enqueue":
        fingerprint = remote_cache_fingerprint(spec, volume)
    directory = output / ".runner" / "jobs"
    manifest = directory / (spec["service"] + ".json")
    def check_existing(key, old, signature):
        if old and (old["intent_hash"] != signature or (fingerprint and old["cache_fingerprint"] != fingerprint)):
            raise ValueError(f"Task/configuration/cache conflict: {key}")

    jobs = read_jobs(manifest)
    keys = []
    for project, scene, prefix, item, signature in selected:
        key = task_key(project, scene, item["id"], spec["kind"])
        keys.append(key)
        check_existing(key, jobs.get(key), signature)
    model = None
    to_collect = []
    for project, scene, prefix, item, signature in selected if mode == "enqueue" else []:
        key = task_key(project, scene, item["id"], spec["kind"])
        with job_lock(manifest, key):
            jobs = read_jobs(manifest)
            check_existing(key, jobs.get(key), signature)
            if key not in jobs:
                job = {"id": item["id"], "project": project, "scene": scene, "prefix": prefix,
                       "kind": spec["kind"], "status": "pending", "intent_hash": signature,
                       "cache_fingerprint": fingerprint,
                       "execution_hash": execution_signature(signature, fingerprint)}
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
                save_job(manifest, key, job)  # Must reach disk BEFORE the network request.
                try:
                    call = model.generate_one.spawn(item, prefix, fingerprint, intent_digest(spec))
                    job.update(call_id=call.object_id, status="queued")
                except BaseException:
                    job["status"] = "uncertain"
                    save_job(manifest, key, job)
                    raise
            save_job(manifest, key, job)
            print(json.dumps({"job": key, **job}, ensure_ascii=False))
    if mode != "enqueue":
        keys = keys if paths else list(read_jobs(manifest))
        signatures = {task_key(p, s, item["id"], spec["kind"]): signature
                      for p, s, _, item, signature in selected}
        for key in dict.fromkeys(keys):
            with job_lock(manifest, key):
                jobs = read_jobs(manifest)
                if key not in jobs:
                    print(json.dumps({"job": key, "status": "not_submitted"}))
                    continue
                job = jobs[key]
                if key in signatures:
                    check_existing(key, job, signatures[key])
                refresh(job, volume, modal.FunctionCall.from_id)
                save_job(manifest, key, job)
                if mode == "collect" and job["status"] == "completed":
                    to_collect.append(job)
                print(json.dumps({"job": key, **job}, ensure_ascii=False))
        if to_collect:
            errors = parallel_collect(to_collect, volume, output)
            # Downloads keep their artifact locks. Merge only collection fields
            # into fresh receipts, preserving concurrent status/call-id updates.
            for collected in to_collect:
                if not collected.get("collected"):
                    continue
                key = task_key(collected["project"], collected["scene"], collected["id"], collected["kind"])
                with job_lock(manifest, key):
                    current = read_jobs(manifest)[key]
                    check_record(current, collected["result"])
                    current.update(collected=True, download_metrics=collected["download_metrics"])
                    save_job(manifest, key, current)
            if errors:
                raise errors[0]
    return read_jobs(manifest)
