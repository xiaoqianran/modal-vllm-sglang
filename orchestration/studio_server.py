"""On-demand localhost bridge to the existing Python SDK and task worker."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import threading
import time
import tempfile
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from orchestration import state, studio
from orchestration.studio_media import serve_file
from pipeline.s01_build_plan import ROOT, build_plan, inherit_archive_layout, new_run

OUTPUT = (ROOT / "outputs").resolve()
RUNTIME = (ROOT / ".runtime").resolve()
INBOX = RUNTIME / "studio-inbox"
MAX_BODY = 1024 * 1024
TASK_NAME = re.compile(r"[0-9]{3}(?:-[a-z0-9]+)+")


def _write_json(path, value):
    from runtime.utils.files import atomic_json
    atomic_json(path, value)


def _plan_from_task(task):
    if not isinstance(task, dict) or not isinstance(task.get("name"), str):
        raise ValueError("Expected a Task JSON object with a name")
    name = task["name"]
    if not TASK_NAME.fullmatch(name):
        raise ValueError("Task name must use NNN-short-description")
    items = task.get("items")
    if isinstance(items, list) and any(
        isinstance(value, dict) and "file" in value
        for item in items if isinstance(item, dict) for value in item.values()
    ):
        raise ValueError("Studio inputs must use a cloud asset reference")
    validation_root = RUNTIME / "studio-validation"
    validation_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="task-", dir=validation_root) as temporary:
        path = Path(temporary) / (name + ".json")
        _write_json(path, task)
        return build_plan(path, root=ROOT)


def _check_delivery(plan):
    if plan.get("delivery", {}).get("provider") not in {"huggingface", "both"}:
        return
    from runtime.delivery import v3
    if plan.get("delivery_protocol") != v3.PROTOCOL:
        return
    from orchestration.delivery_preflight import check_delivery
    check_delivery(plan["delivery"]["bucket"], plan["delivery_app"], plan["delivery_volume"], plan["volume"])


def _task_directories():
    root = state.runtime_root(OUTPUT) / "tasks"
    for path in sorted(root.glob("*/*/plan.json")):
        directory = path.parent
        try:
            if state.layout(directory).get("output") == str(OUTPUT):
                yield directory
        except (OSError, ValueError):
            continue


def _find_task(task_id):
    if not TASK_NAME.fullmatch(task_id):
        raise ValueError("Invalid task ID")
    matches = []
    for directory in _task_directories():
        plan = studio.read_json(directory / "plan.json", {})
        if plan.get("name") == task_id:
            matches.append(directory)
    if len(matches) > 1:
        raise RuntimeError("Task name is ambiguous; use a unique task name")
    return matches[0] if matches else None


def _cloud_owned(plan, current):
    """Every item is accounted for and the remaining cloud work can run detached."""
    from runtime.delivery import destinations, v3

    needs_archive = "huggingface" in destinations(plan)
    records = current["jobs"]
    cloud_runs = current.get("cloud_runs", {})
    for job in plan["jobs"]:
        record = records[job["key"]]
        if record["status"] == "failed":
            continue
        archive_detached = (not needs_archive or record.get("cloud_archive") or
                            (plan.get("delivery_protocol") == v3.PROTOCOL and record.get("delivery_id")))
        if record["status"] == "completed" and archive_detached:
            continue
        if record["status"] in {"submitting", "uncertain"}:
            return False
        if cloud_runs:
            receipt = cloud_runs.get(job["item"], {})
            if (receipt.get("status") in {"queued", "running"}
                    and (receipt.get("controller_state") == "active"
                         or (receipt.get("durable_plan") and receipt.get("call_id")))
                    and (archive_detached or plan.get("delivery_protocol") == v3.PROTOCOL)):
                continue
        elif record.get("call_id") and archive_detached:
            continue
        return False
    return any(record["status"] != "failed" for record in records.values())


def _task_status(directory):
    from orchestration.sdk import snapshot
    from orchestration.protocol import report
    plan, current = snapshot(directory)
    summary = report(plan, current["jobs"])
    owned = _cloud_owned(plan, current)
    records = list(current["jobs"].values())
    receipts = [*records, *current.get("cloud_runs", {}).values()]
    terminal = all(r["status"] in {"completed", "failed"} for r in records)
    locally_pending = any(
        r["status"] != "failed" and not r.get("local_artifact") for r in records
    ) and plan.get("delivery", {}).get("provider", "local") in {"local", "both"}
    failed = any(r["status"] == "failed" for r in records)
    blocked_archive = any("Durable archive blocked:" in (r.get("archive_error") or "") for r in records)
    if terminal and not summary.get("delivery_pending"):
        status = "failed" if failed else "completed"
    elif any(r["status"] == "uncertain" for r in receipts):
        status = "submission_uncertain"
    elif terminal:
        status = "delivering"
    elif owned:
        status = "cloud_owned"
    elif any(r["status"] in {"submitting", "queued", "running", "completed"} for r in receipts):
        status = "submitting"
    else:
        status = "local_registered"
    return {"task_id": plan["name"], "project": plan["project"], "scene": plan["scene"],
            "status": status, "archive_blocked": blocked_archive, "cloud_owned": owned and not terminal,
            "safe_to_close": (owned or terminal and all(r["status"] == "failed" for r in records))
                             and not locally_pending and status != "submission_uncertain",
            "recovery_required": not terminal and any(
                r.get("controller_state") == "terminal" and r.get("status") == "uncertain"
                for r in current.get("cloud_runs", {}).values()
            ),
            "counts": summary["counts"], "total": len(plan["jobs"]),
            "outputs": summary["outputs"], "download_errors": summary.get("download_errors", 0),
            "archive_errors": summary.get("archive_errors", 0),
            "delivery_pending": summary.get("delivery_pending", 0),
            "jobs": [{"key": key, **value} for key, value in current["jobs"].items()]}


def _catalog():
    # Native works are discovered through the HF directory endpoints instead.
    # Keep the old catalogue only for legacy/local assets until migration is validated.
    from runtime.archive.config import DEFAULT_ASSET_BUCKET
    from runtime.delivery.config import configured_delivery
    bucket = configured_delivery(ROOT).get("bucket", DEFAULT_ASSET_BUCKET)
    data = studio.collect(OUTPUT, thumbs=False, native_bucket=bucket)
    by_digest = {}
    for directory in _task_directories():
        try:
            from orchestration.sdk import snapshot
            plan, current = snapshot(directory)
            for job in plan["jobs"]:
                record = current["jobs"][job["key"]]
                artifact = record.get("artifacts", {}).get("image")
                if artifact:
                    by_digest[artifact["sha256"]] = artifact
                    by_digest[(plan["name"], job["key"])] = artifact
        except (OSError, ValueError, KeyError, TypeError):
            continue
    for asset in data["assets"]:
        artifact = by_digest.get(asset.get("digest"))
        if artifact is None:
            parts = asset.get("id", "").split("/", 1)
            if len(parts) == 2:
                artifact = by_digest.get((parts[0], parts[1]))
        if artifact:
            asset["artifact"] = {k: artifact[k] for k in ("path", "sha256", "size")}
    return data


class StudioWorker:
    """One lazy local worker; reuses the existing tick_shot and SDK machinery."""
    def __init__(self, on_change=None):
        self.on_change = on_change or (lambda: None)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.tasks = {}
        self.errors = {}
        self.thread = None

    def add(self, task_id, directory):
        with self.lock:
            self.tasks[task_id] = Path(directory)
            self.errors.pop(task_id, None)
            if self.thread is None or not self.thread.is_alive():
                self.stop_event.clear()
                self.thread = threading.Thread(target=self._run, name="studio-task-worker", daemon=True)
                self.thread.start()

    def _run(self):
        from pipeline.s02_submit_shots import worker

        try:
            worker(OUTPUT, stop=self.stop_event, selection=self.active_directories,
                   observed=self._observed, before_scene=self._preflight)
        except Exception as exc:
            with self.lock:
                for task_id in self.tasks:
                    self.errors[task_id] = {"worker": str(exc)[:500]}

    @staticmethod
    def _preflight(directory):
        from orchestration.sdk import snapshot
        plan, current = snapshot(directory)
        if any(current["jobs"][step["key"]]["status"] in {"pending", "blocked"} and
               not current.get("cloud_runs", {}).get(step["item"])
               for step in plan["jobs"]):
            _check_delivery(plan)

    def _observed(self, directory, item, error):
        with self.lock:
            task_id = next((key for key, path in self.tasks.items() if path == directory), None)
            if task_id is None:
                return
            errors = self.errors.setdefault(task_id, {})
            if error:
                errors[item] = str(error)[:300]
                return
            errors.pop(item, None)
            # A completed original does not imply its optional derivative or
            # explicit local recheck has settled.
            from orchestration.sdk import snapshot
            if not state.pending_items(*snapshot(directory)):
                self.tasks.pop(task_id, None)
                self.errors.pop(task_id, None)
                self.on_change()

    def error(self, task_id):
        with self.lock:
            return " / ".join(f"{item}: {message}" for item, message in self.errors.get(task_id, {}).items())

    def active_directories(self):
        with self.lock:
            return tuple(self.tasks.values())

    def close(self):
        self.stop_event.set()
        thread = self.thread
        if thread and thread.is_alive():
            thread.join(timeout=8)


class StudioService:
    def __init__(self, *, output=OUTPUT):
        global OUTPUT
        OUTPUT = Path(output).resolve()
        self.output = OUTPUT
        self.directories = {}
        self.worker = StudioWorker(self.invalidate_catalog)
        self.session_token = secrets.token_urlsafe(32)
        self.catalog_cache = None
        self.catalog_time = 0.0

    def invalidate_catalog(self):
        self.catalog_cache = None
        self.catalog_time = 0.0

    def validate(self, task):
        plan = _plan_from_task(task)
        return {"valid": True, "name": plan["name"], "workflow": plan["workflow"],
                "project": plan["project"], "scene": plan["scene"], "jobs": len(plan["jobs"]),
                "dag": [{"key": job["key"], "uses": job["uses"],
                         "depends_on": sorted({value["job"] for value in job["inputs"].values()
                                               if isinstance(value, dict) and "job" in value})}
                        for job in plan["jobs"]],
                "services": list(plan["services"]), "delivery": plan.get("delivery", {"provider": "local"}),
                "plan_hash": plan["hash"]}

    def submit(self, task):
        plan = _plan_from_task(task)
        directory = state.runtime_root(OUTPUT) / "tasks" / plan["project"] / plan["scene"]
        plan = inherit_archive_layout(plan, directory)
        inbox_path = INBOX / (plan["name"] + ".json")
        with state.lock(INBOX, filename=".registration.lock", timeout=10):
            if inbox_path.exists() and studio.read_json(inbox_path) != task:
                raise FileExistsError("This task name already belongs to a different Task JSON")
            if (directory / "plan.json").exists():
                existing = studio.read_json(directory / "plan.json", {})
                if existing.get("hash") != plan["hash"]:
                    raise FileExistsError("This project/scene already contains a different task")
                if not (directory / "layout.json").is_file():
                    directory = new_run(plan, root=ROOT, output=OUTPUT)
            else:
                directory = new_run(plan, root=ROOT, output=OUTPUT)
            _write_json(directory / "task.json", task)
            _write_json(inbox_path, task)
            self.directories[plan["name"]] = directory
            self.invalidate_catalog()
            result = _task_status(directory)
            from orchestration.sdk import snapshot
            if state.pending_items(*snapshot(directory)):
                self.worker.add(plan["name"], directory)
            return self._studio_status(directory, result)

    def _directory(self, task_id):
        directory = self.directories.get(task_id)
        if directory is None or not (directory / "plan.json").is_file():
            directory = _find_task(task_id)
            if directory is not None:
                self.directories[task_id] = directory
        return directory

    def resume(self, task_id, *, recover=False):
        directory = self._directory(task_id)
        if directory is None:
            raise FileNotFoundError(task_id)
        from orchestration.sdk import request_resume
        if recover:
            with state.lock(directory, timeout=10):
                current = studio.read_json(directory / "state.json")
                for receipt in current.get("cloud_runs", {}).values():
                    if receipt.get("controller_state") == "terminal" and receipt.get("status") == "uncertain":
                        receipt["recovery_requested"] = True
                state.save(directory / "state.json", current)
        request_resume(directory)
        from orchestration.sdk import snapshot
        if state.pending_items(*snapshot(directory)):
            self.worker.add(task_id, directory)
        return self._studio_status(directory, _task_status(directory))

    def _studio_status(self, directory, result):
        active_dirs = set(self.worker.active_directories())
        if result["safe_to_close"]:
            active_dirs.update(_task_directories())
        try:
            result["studio_safe_to_close"] = result["safe_to_close"] and all(
                _task_status(active)["safe_to_close"] for active in active_dirs
                if active != directory
            )
        except (ValueError, OSError, RuntimeError, KeyError):
            result["studio_safe_to_close"] = False
        return result

    def task(self, task_id):
        directory = self._directory(task_id)
        if directory is None:
            raise FileNotFoundError(task_id)
        result = _task_status(directory)
        error = self.worker.error(task_id)
        if error:
            result["worker_error"] = error
        return self._studio_status(directory, result)

    def tasks(self):
        result = []
        for directory in _task_directories():
            try:
                result.append(_task_status(directory))
            except (OSError, ValueError, KeyError):
                continue
        return result

    def catalog(self):
        now = time.monotonic()
        if self.catalog_cache is None or now - self.catalog_time >= 15:
            self.catalog_cache = _catalog()
            self.catalog_time = now
        return self.catalog_cache

    def works_bucket(self):
        from runtime.archive.config import DEFAULT_ASSET_BUCKET
        from runtime.delivery.config import configured_delivery
        return configured_delivery(ROOT).get("bucket", DEFAULT_ASSET_BUCKET)

    def works(self):
        from orchestration.studio_works import list_works
        return {"works": list_works(self.works_bucket())}

    def work(self, identity):
        from orchestration.studio_works import work_detail
        bucket = self.works_bucket()
        data = work_detail(bucket, identity)
        # Only attach a live Volume reference if the existing local execution
        # record proves the same work and step. Browsing needs no such record.
        from runtime.archive.works import media_path, work_id
        from runtime.archive.huggingface import public_url
        from orchestration.sdk import snapshot
        try:
            directory = self._directory(data["name"])
            if directory is None:
                return data
            plan, current = snapshot(directory)
            if (plan.get("work") or {}).get("id", work_id(plan)) != identity:
                return data
            data["local_task_id"] = f"{plan['project']}/{plan['scene']}"
            for asset in data["assets"]:
                key = asset["id"].split("/", 1)[1]
                step, item = key.split("/", 1)
                record = current["jobs"].get(key, {})
                row = record.get("cloud_archive") or {}
                artifact = record.get("artifacts", {}).get("image")
                if (artifact and row.get("status") == "archived" and row.get("bucket") == bucket
                        and row.get("verification") == "sha256"
                        and row.get("stored_bytes") == asset.get("bytes")
                        and row.get("original_bytes") == artifact.get("size")
                        and row.get("original_sha256") == artifact.get("sha256")
                        and asset["url"] == public_url(bucket, media_path(
                            {"id": identity, "item": item, "step": step},
                            PurePosixPath(row["path"]).suffix))):
                    asset["artifact"] = {k: artifact[k] for k in ("path", "sha256", "size")}
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            pass  # Optional edit-source metadata cannot block gallery reading.
        return data

    def render_home(self):
        from runtime.utils.files import atomic_write
        from orchestration.studio_workflows import image_workflows
        data = {"assets": [], "tasks": [], "live_studio": True, "remote_works": True,
                "bucket_url": "https://huggingface.co/buckets/" + self.works_bucket(),
                "image_workflows": image_workflows(ROOT), "view": "atelier", "selected": None}
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        if studio.TEMPLATE.read_text(encoding="utf-8").count("__STUDIO_DATA__") != 1:
            raise RuntimeError("Invalid Vue Studio build; run npm run build")
        atomic_write(OUTPUT / "atelier.html",
                     studio.TEMPLATE.read_text(encoding="utf-8").replace("__STUDIO_DATA__", payload).encode("utf-8"))
        return OUTPUT / "atelier.html"


class StudioHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, service):
        self.service = service
        super().__init__(address, StudioHandler)


class StudioHandler(BaseHTTPRequestHandler):
    server_version = "LocalStudio/1"

    def log_message(self, fmt, *args):
        if os.environ.get("STUDIO_ACCESS_LOG") == "1":
            super().log_message(fmt, *args)

    @property
    def service(self):
        return self.server.service

    def _headers(self, content_type, length, *, cookie=None):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store" if self.path.startswith("/api/") else "no-cache")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _json(self, value, status=200, *, cookie=None):
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _error(self, status, message):
        self._json({"error": message}, status)

    def _valid_host(self):
        host = self.headers.get("Host", "")
        return host in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

    def _valid_origin(self):
        origin = self.headers.get("Origin")
        return origin is None or origin in {
            f"http://127.0.0.1:{self.server.server_port}",
            f"http://localhost:{self.server.server_port}",
        }

    def _authorized_write(self):
        if not self._valid_host() or not self._valid_origin():
            return False
        cookie = self.headers.get("Cookie", "")
        token = next((part.strip().partition("=")[2] for part in cookie.split(";")
                      if part.strip().startswith("studio_session=")), "")
        header = self.headers.get("X-Studio-Token", "")
        return (bool(token) and hmac.compare_digest(token, self.service.session_token)
                and hmac.compare_digest(header, self.service.session_token))

    def _body(self):
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Content-Type must be application/json")
        raw = self.headers.get("Content-Length")
        if raw is None or not raw.isdigit() or int(raw) > MAX_BODY:
            raise ValueError("Request body is missing or exceeds 1 MiB")
        data = self.rfile.read(int(raw))
        return json.loads(data.decode("utf-8"))

    def do_GET(self):
        if not self._valid_host():
            return self._error(403, "Invalid Host")
        path = urlsplit(self.path).path
        try:
            if path == "/api/session":
                cookie = f"studio_session={self.service.session_token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200"
                return self._json({"token": self.service.session_token}, cookie=cookie)
            if path == "/api/health":
                return self._json({"ready": True, "worker_running": bool(self.service.worker.thread and self.service.worker.thread.is_alive())})
            if path == "/api/works":
                return self._json(self.service.works())
            if path.startswith("/api/works/"):
                return self._json(self.service.work(unquote(path.removeprefix("/api/works/"))))
            if path == "/api/catalog":
                return self._json(self.service.catalog())
            if path == "/api/tasks":
                return self._json({"tasks": self.service.tasks()})
            if path == "/api/models":
                from orchestration.model_capabilities import CAPABILITIES
                from orchestration.studio_workflows import image_workflows
                return self._json({"capabilities": CAPABILITIES, "image_workflows": image_workflows(ROOT)})
            if path.startswith("/api/tasks/"):
                task_id = unquote(path.removeprefix("/api/tasks/"))
                if "/" in task_id:
                    return self._error(404, "Task not found")
                return self._json(self.service.task(task_id))
            if path in {"/", "/atelier.html"}:
                html = self.service.render_home().read_bytes()
                return self._send_bytes(html, "text/html; charset=utf-8")
            return self._serve_local(path)
        except FileNotFoundError:
            return self._error(404, "Not found")
        except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
            return self._error(400, str(exc))

    def do_HEAD(self):
        if not self._valid_host():
            return self._error(403, "Invalid Host")
        try:
            return self._serve_local(urlsplit(self.path).path)
        except FileNotFoundError:
            return self._error(404, "Not found")
        except OSError:
            return self._error(400, "Unable to read file")

    def _send_bytes(self, data, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _serve_local(self, url_path):
        decoded = unquote(url_path).lstrip("/")
        if not decoded or "\\" in decoded or ":" in decoded:
            return self._error(404, "Not found")
        pure = PurePosixPath(decoded)
        if pure.is_absolute() or ".." in pure.parts:
            return self._error(404, "Not found")
        if decoded.startswith(".runtime/monitor/") and pure.suffix == ".js":
            root = state.runtime_root(OUTPUT) / "monitor"
            relative = PurePosixPath(*pure.parts[2:])
            path = (root / Path(*relative.parts)).resolve()
            allowed = root.resolve()
            content_type = "text/javascript; charset=utf-8"
        elif decoded == ".runtime/studio-catalog.js":
            path = (OUTPUT / decoded).resolve()
            allowed = OUTPUT.resolve()
            content_type = "text/javascript; charset=utf-8"
        else:
            if pure.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".webm"}:
                return self._error(404, "Not found")
            path = (OUTPUT / Path(*pure.parts)).resolve()
            allowed = OUTPUT.resolve()
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if not path.is_relative_to(allowed) or not path.is_file():
            return self._error(404, "Not found")
        return serve_file(self, path, content_type)

    def do_POST(self):
        if not self._valid_host() or not self._valid_origin():
            return self._error(403, "Invalid origin")
        if not self._authorized_write():
            return self._error(403, "Open Studio in this browser session first")
        path = urlsplit(self.path).path
        try:
            if path == "/api/tasks/validate":
                return self._json(self.service.validate(self._body()))
            if path == "/api/tasks":
                result = self.service.submit(self._body())
                return self._json(result, 202)
            if path.startswith("/api/tasks/") and path.endswith("/resume"):
                task_id = unquote(path.removeprefix("/api/tasks/").removesuffix("/resume").strip("/"))
                body = self._body()
                if body == {"recover": True}:
                    return self._json(self.service.resume(task_id, recover=True), 202)
                if body != {}:
                    raise ValueError("Invalid resume request")
                return self._json(self.service.resume(task_id), 202)
            return self._error(404, "Not found")
        except FileExistsError as exc:
            return self._error(409, str(exc))
        except FileNotFoundError as exc:
            return self._error(404, str(exc))
        except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
            return self._error(400, str(exc))

    def log_error(self, status, message):
        self._error(status, message)


def serve(*, host="127.0.0.1", port=18765, open_browser=True, output=OUTPUT):
    if host != "127.0.0.1":
        raise ValueError("Studio only binds to 127.0.0.1")
    service = StudioService(output=output)
    service.render_home()
    server = StudioHTTPServer((host, port), service)
    url = f"http://{host}:{server.server_port}/"
    print(f"Studio: {url}", flush=True)
    print("Local Python SDK bridge is idle until a task is submitted.", flush=True)
    if open_browser:
        threading.Timer(.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt:
        print("\\nStopping local Studio; accepted Modal calls continue remotely.", flush=True)
    finally:
        server.shutdown()
        server.server_close()
        service.worker.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--output", default="outputs")
    args = parser.parse_args(argv)
    serve(host=args.host, port=args.port, open_browser=not args.no_browser, output=args.output)


if __name__ == "__main__":
    main()
