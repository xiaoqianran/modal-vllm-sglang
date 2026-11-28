"""Measured dashboard data; missing measurements stay unknown, never zero."""
from collections import Counter
from runtime.archive.preview import verified_preview
from datetime import datetime, timezone
import math
from pathlib import Path
from urllib.parse import quote

from orchestration import state
from runtime.tasks.task_identity import intent_digest
from runtime.utils.files import read_json

STAGES = {"register": "登记", "submit": "提交", "wait": "等待", "startup": "启动",
          "generate": "生成", "save": "云端保存", "transfer": "素材交接", "download": "下载到本地"}
STATUS = {"pending": "待调度", "blocked": "等待中", "queued": "已提交", "submitting": "提交中",
          "completed": "已完成", "failed": "失败", "uncertain": "状态待确认", "running": "运行中",
          "delivering": "交付中",
          "ready": "已就绪", "preparing": "检查准备", "prepare_service": "部署 CPU 准备服务",
          "weights": "权重下载与校验", "cache_check": "检查缓存指纹", "build_deploy": "构建并部署推理服务",
          "deploying": "部署中", "downloading": "下载中"}


def stamp(value):
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value) if math.isfinite(value) else None
        parsed = datetime.fromisoformat(value)
        return parsed.timestamp() if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def elapsed(start, end):
    return max(0, (end - start) * 1000) if start is not None and end is not None else None


def read_optional(path):
    try:
        return read_json(path)
    except (OSError, ValueError):
        return {}


def stage_rows(spans, now):
    rows = []
    for stage, label in STAGES.items():
        matches = [s for s in spans if s.get("stage") == stage]
        measured = [s for s in matches if s.get("status") != "running" and s.get("duration_ms") is not None]
        running = [s for s in matches if s.get("status") == "running"]
        duration = sum(s["duration_ms"] for s in measured) if measured else None
        if running:
            partial = [elapsed(stamp(s.get("started_at")), now) for s in running]
            known = [v for v in partial if v is not None]
            duration = (duration or 0) + sum(known) if known else duration
        sources = sorted({s.get("measurement", "unknown") for s in matches})
        rows.append({"stage": stage, "label": label, "duration_ms": duration,
                     "status": "running" if running else "measured" if measured else "unknown",
                     "source": ", ".join(sources) or "unknown",
                     "reasons": sorted({s["reason"] for s in matches if s.get("reason")})})
    return rows


def build_snapshot(directory, plan, current, dimensions, *, archive_receipts=None):
    output = Path(state.layout(directory)["output"])
    runtime = state.runtime_root(output)
    task_id = f"{plan['project']}/{plan['scene']}"
    report = read_optional(runtime / "logs/.telemetry/reports" / (intent_digest(task_id) + ".json"))
    if report.get("task_id") != task_id:
        report = {}
    now = datetime.now(timezone.utc).timestamp()
    spans = report.get("stages", [])
    # Revalidating an already downloaded artifact is not a new download or a new completion.
    spans = [s for s in spans if not (s.get("stage") == "download" and s.get("attributes", {}).get("reused"))]
    records = current["jobs"]
    assets = {}
    archive_receipts = archive_receipts or {}
    preview_index = read_optional(output / ".runtime/preview-index.json")
    for job in plan["jobs"]:
        record = records[job["key"]]
        artifact = record.get("local_artifact")
        if artifact:
            path = state.media_directory(directory) / artifact["path"]
            try:
                relative = path.relative_to(output).as_posix()
            except ValueError:
                relative = None
            receipt = archive_receipts.get(relative)
            if (receipt and receipt.get("status") == "archived"
                    and receipt.get("verification") == "sha256"
                    and artifact.get("sha256") == receipt.get("original_sha256")
                    and artifact.get("size") == receipt.get("original_bytes")):
                from runtime.archive.huggingface import public_url
                try:
                    href = public_url(receipt["bucket"], receipt["path"])
                    if href != receipt.get("url"):
                        raise ValueError("Archive URL mismatch")
                    assets[job["key"]] = {"url": href, "local_url": quote(relative),
                                           "filename": path.name, "kind": artifact["type"],
                                           "dimensions": dimensions(path) if path.is_file() else None,
                                           "bytes": receipt["stored_bytes"], "thumb": verified_preview(receipt, preview_index) or (href if artifact["type"] == "image" else "")}
                    continue
                except (KeyError, TypeError, ValueError):
                    pass
            if path.is_file():
                try:
                    href = quote(path.relative_to(output).as_posix())
                except ValueError:
                    pass
                else:
                    assets[job["key"]] = {"url": href, "filename": path.name, "kind": artifact["type"],
                                           "dimensions": dimensions(path), "bytes": path.stat().st_size}
                    continue
        archived = record.get("cloud_archive")
        if archived and archived.get("status") == "archived":
            # Only use the trusted HF URL generated by our archive adapter.
            from runtime.archive.huggingface import public_url
            try:
                href = public_url(archived["bucket"], archived["path"])
            except (KeyError, ValueError, TypeError):
                continue
            assets[job["key"]] = {"url": href,
                                   "filename": archived["path"].rsplit("/", 1)[-1],
                                   "kind": archived["kind"], "dimensions": None,
                                   "bytes": archived["stored_bytes"], "thumb": verified_preview(archived, preview_index) or (href if archived["kind"] == "image" else "")}
    jobs, shots = [], {}
    for job in plan["jobs"]:
        key, record = job["key"], records[job["key"]]
        matching = [s for s in spans if s.get("job_key") == key and
                    (not s.get("call_id") or not record.get("call_id") or s["call_id"] == record["call_id"])]
        pending = []
        for field, reason in (("model_wait_started_at", "model"), ("dependency_wait_started_at", "dependency")):
            if record.get(field):
                pending.append({"stage": "wait", "status": "running", "started_at": record[field],
                                "measurement": "derived", "reason": reason})
        reason = record.get("error") or record.get("download_error") or record.get("archive_error") or (
            "等待模型准备" if record.get("wait_reason") == "model" else
            "等待首尾帧等输入素材" if record["status"] == "blocked" else
            "已提交远程调用，平台排队时间未采集" if record["status"] == "queued" else "")
        asset = assets.get(key)
        if asset and asset.get("dimensions"):
            for consumer in plan["jobs"]:
                if not any(isinstance(ref, dict) and ref.get("job") == key for ref in consumer["inputs"].values()):
                    continue
                video = assets.get(consumer["key"])
                if video and video["kind"] == "video" and video.get("dimensions") != asset["dimensions"]:
                    dims = video.get("dimensions")
                    if dims:
                        reason = f"尺寸不匹配：视频为 {dims[0]} × {dims[1]}，首尾帧需重新生成"
        stages = stage_rows(matching + pending, now)
        download_spans = [s for s in matching if s.get("stage") == "download" and s.get("status") == "completed"
                          and s.get("attributes", {}).get("downloaded_bytes") is not None]
        transferred = sum(s["attributes"]["downloaded_bytes"] for s in download_spans) if download_spans else None
        download_ms = sum(s.get("duration_ms") or 0 for s in download_spans)
        starts = [stamp(s.get("started_at")) for s in matching] + [stamp(record.get("submitted_at"))]
        ends = [stamp(s.get("finished_at")) for s in matching] + [stamp(record.get("local_collected_at")), stamp(record.get("archive_collected_at")), stamp(record.get("finished_at"))]
        starts, ends = [x for x in starts if x is not None], [x for x in ends if x is not None]
        from runtime.delivery import destinations
        finished = record["status"] == "failed" or (record["status"] == "completed" and all(
            record.get("local_artifact" if dest == "local" else "cloud_archive")
            for dest in destinations(plan)))
        row = {"key": key, "step": job["step"], "item": job["item"], "status": record["status"],
               "status_label": STATUS.get(record["status"], record["status"]), "reason": reason, "asset": asset,
               "requested": job["params"].get("size") or job["params"].get("target"),
               "stages": stages, "elapsed_ms": elapsed(min(starts) if starts else None, max(ends) if ends and finished else now),
               "started_at": min(starts) if starts else None, "finished_at": max(ends) if ends and finished else None,
               "elapsed_label": "已观测耗时", "execution_ms": record.get("result", {}).get("execution_seconds"),
               "transferred_bytes": transferred, "download_bps": transferred / (download_ms / 1000) if transferred is not None and download_ms else None,
               "prompt": job["inputs"].get("prompt"), "parameters": job["params"], "call_id": record.get("call_id"),
               "dependencies": [ref["job"] for ref in job["inputs"].values() if isinstance(ref, dict) and "job" in ref]}
        if row["execution_ms"] is not None:
            row["execution_ms"] *= 1000  # Service execution includes transfer/fetch; not labelled pure inference.
        jobs.append(row)
        shots.setdefault(job["item"], []).append(row)
    counts = dict(Counter(r["status"] for r in records.values()))
    terminal = all(r["status"] in {"completed", "failed"} for r in records.values())
    complete = terminal and all(r["status"] == "failed" or key in assets for key, r in records.items())
    # A registered or accepted task is not necessarily running. Derive the
    # display status from recorded evidence, without polling Modal or editing history.
    if terminal:
        task_status = "failed" if counts.get("failed") else "completed" if complete else "delivering"
    else:
        unfinished = {job["item"] for job in plan["jobs"]
                      if records[job["key"]]["status"] not in {"completed", "failed"}}
        controllers = [r for item, r in current.get("cloud_runs", {}).items() if item in unfinished]
        if counts.get("uncertain") or any(
            r.get("status") in {"uncertain", "failed", "completed"}
            or r.get("controller_state") in {"terminal", "unconfirmed"}
            or (r.get("call_id") and not r.get("durable_plan") and not r.get("controller_state"))
            for r in controllers
        ):
            task_status = "uncertain"
        elif counts.get("running") or any(
            r.get("status") == "running" and r.get("controller_state") == "active" for r in controllers
        ):
            task_status = "running"
        elif counts.get("queued") or counts.get("submitting") or any(
            r.get("status") in {"queued", "submitting", "running"} for r in controllers
        ):
            task_status = "queued"
        else:
            task_status = "pending"
    start = stamp(current.get("registered_at"))
    timing_source = "登记至本地收集完成" if start is not None else "历史记录：从首次已知提交计算"
    if start is None:
        candidates = [stamp(r.get("submitted_at")) for r in records.values()]
        start = min((x for x in candidates if x is not None), default=None)
    end = stamp(current.get("finished_at"))
    if end is None and complete:
        candidates = [stamp(s.get("finished_at")) for s in spans if s.get("job_key") in records]
        candidates += [stamp(r.get("local_collected_at")) for r in records.values()]
        end = max((x for x in candidates if x is not None), default=None)
    if not complete:
        end = now
    models = []
    for name in plan["services"]:
        model = read_optional(runtime / "models" / (name + ".json"))
        models.append({"name": name, "status": model.get("status", "unknown"),
                       "label": STATUS.get(model.get("status"), "未采集"), "error": model.get("error"),
                       "attempt": model.get("attempt"), "updated_at": model.get("updated_at"),
                       "stages": model.get("stages", []), "active_stage": model.get("active_stage"),
                       "downloaded_bytes": model.get("downloaded_bytes"), "total_bytes": model.get("total_bytes"),
                       "bytes_per_second": model.get("bytes_per_second"),
                       "elapsed_ms": elapsed(stamp(model.get("started_at")), stamp(model.get("finished_at")) or now),
                       "log_url": quote(f"../.runtime/logs/models/{name}.log") if (runtime / "logs/models" / (name + ".log")).is_file() else None})
    shot_rows = []
    for item, group in shots.items():
        beginnings = [j["started_at"] for j in group if j["started_at"] is not None]
        endings = [j["finished_at"] for j in group if j["finished_at"] is not None]
        done = all(j["status"] in {"completed", "failed"} for j in group)
        shot_rows.append({"item": item, "keys": [j["key"] for j in group],
                          "elapsed_ms": elapsed(min(beginnings) if beginnings else None,
                                                max(endings) if done and endings else now)})
    return {"name": plan["name"], "task_id": task_id, "updated_at": now, "status": task_status,
            "status_label": STATUS.get(task_status, task_status), "counts": counts,
            "total": len(jobs), "downloaded": len(assets), "render": plan.get("render"),
            "started_at": start, "finished_at": end if complete else None, "elapsed_ms": elapsed(start, end),
            "timing_source": timing_source, "jobs": jobs, "shots": shot_rows,
            "stages": stage_rows([s for s in spans if s.get("task_id") == task_id], now),
            "shared_startup": stage_rows([s for s in spans if s.get("stage") == "startup" and not s.get("job_key")], now),
            "models": models, "telemetry_updated_at": report.get("event_count"),
            "log_url": quote(f"../.runtime/logs/tasks/{plan['project']}_{plan['scene']}.jsonl"),
            "notes": ["阶段累计可能并行，不能相加当作任务总耗时。", "共用准备、启动单列，不重复计入每个镜头。",
                      "平台排队、快照恢复、权重字节速度没有证据时显示未采集。",
                      "历史任务保留原始尺寸与时间；缺失阶段不会补造。"]}
