"""Local Studio API: offline validation, loopback security and ownership semantics."""
import http.client
import json
import shutil
import subprocess
import sys
import threading
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest

from orchestration.studio_server import (
    StudioHTTPServer, StudioService, _cloud_owned, _plan_from_task, _task_status
)
from orchestration import studio_server
from pipeline.s02_submit_shots import claim_scene
from runtime.delivery import v3
from runtime.telemetry.recorder import close_local


def task(name="099-local-studio-test"):
    return {
        "version": 1,
        "name": name,
        "workflow": "moss-t2i",
        "render": {"aspect_ratio": "16:9", "resolution": "2k"},
        "delivery": {"provider": "local"},
        "items": [{"id": "001", "prompt": "A quiet mountain lake"}],
    }


class FakeService:
    def __init__(self):
        self.session_token = "local-test-token"
        self.worker = type("Worker", (), {"thread": None})()
        self.validation = None

    def validate(self, value):
        self.validation = value
        return {"valid": True, "jobs": 1}

    def submit(self, value):
        return {"task_id": value["name"], "status": "local_registered", "cloud_owned": False}

    def resume(self, task_id):
        return {"task_id": task_id}

    def task(self, task_id):
        return {"task_id": task_id, "cloud_owned": True}

    def tasks(self):
        return []

    def catalog(self):
        return {"assets": [], "tasks": []}


@pytest.fixture
def server():
    service = FakeService()
    instance = StudioHTTPServer(("127.0.0.1", 0), service)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield instance, service
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=2)


def request(server, method, path, body=None, *, token=None, cookie=None, origin=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    headers = {"Host": f"127.0.0.1:{server.server_port}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-Studio-Token"] = token
    if cookie:
        headers["Cookie"] = cookie
    if origin is not None:
        headers["Origin"] = origin
    data = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=data, headers=headers)
    response = conn.getresponse()
    raw = response.read()
    result = json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw
    headers = response.getheaders()
    conn.close()
    return response.status, result, headers


def test_validate_is_offline_and_uses_real_task_planner(monkeypatch):
    service = StudioService()
    result = service.validate(task())
    assert result["valid"] is True
    assert result["workflow"] == "moss-t2i"
    assert result["jobs"] == 1
    assert result["services"] == ["moss"]
    assert result["dag"] == [{"key": "image/001", "uses": "moss.t2i", "depends_on": []}]
    assert not (service.output / "099-local-studio-test").exists()


def test_task_name_rejects_path_traversal():
    with pytest.raises(ValueError):
        _plan_from_task(task("../outside"))


def test_studio_rejects_local_input_paths():
    definition = task()
    definition["items"][0]["image"] = {"file": "secret.png"}
    with pytest.raises(ValueError, match="cloud asset reference"):
        _plan_from_task(definition)


def test_session_cookie_and_same_origin_write(server):
    instance, service = server
    status, session, headers = request(instance, "GET", "/api/session")
    assert status == 200
    cookie = next(value for key, value in headers if key.lower() == "set-cookie")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    status, result, _ = request(
        instance, "POST", "/api/tasks/validate", task(),
        token=session["token"], cookie=cookie.split(";", 1)[0],
        origin=f"http://127.0.0.1:{instance.server_port}")
    assert status == 200 and result["valid"]
    assert service.validation == task()


def test_write_requires_origin_and_session_token(server):
    instance, _ = server
    status, _, _ = request(instance, "POST", "/api/tasks/validate", task(),
                           token="local-test-token",
                           cookie="studio_session=local-test-token",
                           origin="http://evil.example")
    assert status == 403
    status, _, _ = request(instance, "POST", "/api/tasks/validate", task(),
                           origin=f"http://127.0.0.1:{instance.server_port}")
    assert status == 403


def test_duplicate_http_submission_registers_once_and_reads_stay_offline(tmp_path, monkeypatch):
    output = tmp_path / "outputs"
    shutil.copytree(studio_server.ROOT / "workflows", tmp_path / "workflows")
    monkeypatch.setattr(studio_server, "ROOT", tmp_path)
    monkeypatch.setattr(studio_server, "OUTPUT", output)
    monkeypatch.setattr(studio_server, "RUNTIME", tmp_path / ".runtime")
    monkeypatch.setattr(studio_server, "INBOX", tmp_path / ".runtime/studio-inbox")
    service = StudioService(output=output)
    register = Mock(wraps=studio_server.new_run)
    monkeypatch.setattr(studio_server, "new_run", register)
    monkeypatch.setattr(service.worker, "add", Mock())
    with StudioHTTPServer(("127.0.0.1", 0), service) as instance:
        thread = threading.Thread(target=instance.serve_forever, daemon=True)
        thread.start()
        try:
            status, catalog, _ = request(instance, "GET", "/api/catalog")
            assert status == 200 and catalog["tasks"] == []
            status, page, _ = request(instance, "GET", "/")
            assert status == 200 and b"__STUDIO_DATA__" not in page
            assert request(instance, "GET", "/api/health")[1]["worker_running"] is False
            service.worker.add.assert_not_called()
            _, session, _ = request(instance, "GET", "/api/session")
            token = session["token"]
            with ThreadPoolExecutor(max_workers=2) as pool:
                def submit(_):
                    return request(instance, "POST", "/api/tasks", task(), token=token,
                                   cookie=f"studio_session={token}")

                replies = list(pool.map(submit, range(2)))
                assert [status for status, _, _ in replies] == [202, 202]
                assert all(not result["safe_to_close"] for _, result, _ in replies)
                register.assert_called_once()
                service.worker.add.reset_mock()
                path = "/api/tasks/" + task()["name"]
                replies = list(pool.map(lambda _: request(instance, "GET", path), range(2)))
                assert all(status == 200 and result["status"] == "local_registered"
                           for status, result, _ in replies)
                service.worker.add.assert_not_called()
        finally:
            instance.shutdown()
            thread.join(timeout=2)
            close_local(output / ".runtime/logs")


def test_cloud_ownership_requires_every_item_receipt():
    plan = {"jobs": [{"item": "001", "key": "a"}, {"item": "002", "key": "b"}]}
    current = {"jobs": {"a": {"status": "pending"}, "b": {"status": "pending"}},
               "cloud_runs": {"001": {"status": "queued", "call_id": "fc-1", "durable_plan": True}}}
    assert not _cloud_owned(plan, current)
    current["cloud_runs"]["002"] = {"status": "queued", "call_id": "fc-2", "durable_plan": True}
    assert _cloud_owned(plan, current)
    current["cloud_runs"]["002"]["status"] = "uncertain"
    assert not _cloud_owned(plan, current)


def test_direct_task_requires_call_receipt():
    plan = {"jobs": [{"item": "001", "key": "a"}, {"item": "002", "key": "b"}]}
    current = {"jobs": {"a": {"status": "queued", "call_id": "fc-a"},
                        "b": {"status": "submitting"}}}
    assert not _cloud_owned(plan, current)
    current["jobs"]["b"] = {"status": "queued", "call_id": "fc-b"}
    assert _cloud_owned(plan, current)


def test_terminal_local_failure_does_not_block_safe_shutdown(monkeypatch):
    plan = {"name": "099-failed", "project": "test", "scene": "test", "outputs": {},
            "jobs": [{"item": "001", "key": "a"}]}
    current = {"jobs": {"a": {"status": "failed"}}}
    monkeypatch.setattr("orchestration.sdk.snapshot", lambda directory: (plan, current))
    result = _task_status(None)
    assert result["status"] == "failed"
    assert result["safe_to_close"]
    assert not result["cloud_owned"]


def test_direct_archive_requires_durable_delivery_intent():
    plan = {"jobs": [{"item": "001", "key": "a"}], "delivery": {"provider": "huggingface"},
            "delivery_protocol": v3.PROTOCOL}
    record = {"status": "queued", "call_id": "fc-a"}
    current = {"jobs": {"a": record}}
    assert not _cloud_owned(plan, current)
    record["delivery_id"] = "delivery-a"
    assert _cloud_owned(plan, current)
    record["status"] = "uncertain"
    assert not _cloud_owned(plan, current)
    record["status"] = "completed"
    assert _cloud_owned(plan, current)


def test_pending_local_collection_is_visible(monkeypatch):
    plan = {"name": "099-test", "project": "test", "scene": "test", "outputs": {},
            "transport": "sdk", "jobs": [{"item": "001", "key": "a"}]}
    current = {"jobs": {"a": {"status": "completed"}}}
    monkeypatch.setattr("orchestration.sdk.snapshot", lambda directory: (plan, current))
    result = _task_status(None)
    assert result["status"] == "delivering"
    assert not result["safe_to_close"]
    current["jobs"]["a"]["local_artifact"] = {"path": "image.png"}
    assert _task_status(None)["status"] == "completed"
    assert _task_status(None)["safe_to_close"]


def test_studio_stays_open_for_another_task(tmp_path, monkeypatch):
    service = StudioService()
    current, other = tmp_path / "current", tmp_path / "other"
    service.worker.tasks["other"] = other
    monkeypatch.setattr(studio_server, "_task_directories", lambda: [other])
    status = Mock(return_value={"safe_to_close": False})
    monkeypatch.setattr(studio_server, "_task_status", status)
    result = service._studio_status(current, {"safe_to_close": True})
    status.assert_called_once_with(other)
    assert not result["studio_safe_to_close"]
    service.worker.tasks.clear()
    assert not service._studio_status(current, {"safe_to_close": True})["studio_safe_to_close"]
    status.return_value["safe_to_close"] = True
    assert service._studio_status(current, {"safe_to_close": True})["studio_safe_to_close"]


def test_scene_claim_is_released_on_process_exit(tmp_path):
    script = (
        "import json, sys\n"
        "sys.path[:] = json.loads(sys.argv[2])\n"
        "from contextlib import ExitStack\n"
        "from pipeline.s02_submit_shots import claim_scene\n"
        "with ExitStack() as stack:\n"
        "    print(claim_scene(stack, sys.argv[1]), flush=True)\n"
        "    sys.stdin.read()\n"
    )
    # Killing the Windows venv launcher alone leaves its interpreter running.
    with subprocess.Popen([sys._base_executable, "-B", "-c", script, str(tmp_path), json.dumps(sys.path)],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as owner:
        try:
            assert owner.stdout.readline().strip() == "True"
            with ExitStack() as competitor:
                assert not claim_scene(competitor, tmp_path)
        finally:
            owner.kill()
            owner.wait(timeout=5)
    with ExitStack() as recovered:
        assert claim_scene(recovered, tmp_path)


def test_interrupted_registration_can_finish_same_plan(tmp_path, monkeypatch):
    from pipeline.s01_build_plan import _new_run
    from orchestration import state
    plan = _plan_from_task(task())
    save = state.save
    def interrupt(path, value):
        if path.name == "plan.json":
            raise OSError("interrupted before publication")
        save(path, value)
    with monkeypatch.context() as mocked:
        mocked.setattr(state, "save", interrupt)
        with pytest.raises(OSError):
            _new_run(plan, root=tmp_path)
    assert not list(tmp_path.rglob("plan.json"))
    directory = _new_run(plan, root=tmp_path)
    assert state.read_json(directory / "state.json")["plan_hash"] == plan["hash"]
    assert not (directory / ".runner/registration.json").exists()


def test_studio_worker_advances_sibling_after_item_error(tmp_path, monkeypatch):
    from pipeline import s02_submit_shots as scheduler
    from orchestration import state
    directory = tmp_path / "scene"
    from runtime.tasks.task_identity import intent_digest
    plan = {"transport": "sdk", "jobs": [{"item": "a", "key": "image/a"},
                                         {"item": "b", "key": "image/b"},
                                         {"item": "finished", "key": "image/finished"}]}
    plan["hash"] = intent_digest(plan)
    records = {job["key"]: {"status": "pending"} for job in plan["jobs"]}
    records["image/finished"] = {"status": "completed", "local_artifact": {"path": "finished.png"}}
    state.save(directory / "state.json", {"plan_hash": plan["hash"], "jobs": records})
    state.save(directory / "plan.json", plan)
    monkeypatch.setattr("orchestration.monitor.render", lambda output: None)
    stop = threading.Event()
    seen = {}
    visited = set()
    preflight = Mock()
    def tick(directory, item, backend, downloads, pool):
        visited.add(item)
        preflight.assert_called_once_with(Path(directory))
        if item == "a":
            raise OSError("offline")
    def observe(directory, item, error):
        seen.setdefault(item, error)
        if len(seen) == 2:
            stop.set()
    monkeypatch.setattr(scheduler, "tick_shot", tick)
    timer = threading.Timer(3, stop.set)
    timer.start()
    try:
        scheduler.worker(tmp_path, poll=0.01, backend=object(), selection=lambda: [directory],
                         observed=observe, stop=stop, before_scene=preflight)
    finally:
        timer.cancel()
    assert isinstance(seen["a"], OSError) and seen["b"] is None
    assert visited == {"a", "b"}


def test_blocked_archive_is_visible_with_detached_execution(monkeypatch):
    plan = _plan_from_task(task())
    plan.update(delivery={"provider": "huggingface"}, delivery_protocol=v3.PROTOCOL, outputs={})
    record = {"status": "completed", "delivery_id": "d", "archive_error": "Durable archive blocked: blocked_auth"}
    monkeypatch.setattr("orchestration.sdk.snapshot", lambda _: (plan, {"jobs": {"image/001": record}}))
    result = _task_status(None)
    assert result["safe_to_close"] and result["archive_blocked"]
    assert result["delivery_pending"] == 1


def test_cross_platform_launchers_exist_and_shell_syntax_is_valid():
    root = Path(__file__).resolve().parents[1]
    assert (root / "start-studio.bat").is_file()
    shell = root / "start-studio.sh"
    assert shell.is_file()
    result = subprocess.run(["sh", "-n", str(shell)], capture_output=True, text=True)
    if result.returncode not in (0, 127):
        pytest.fail(result.stderr)
    assert "python run.py studio" in (root / "start-studio.bat").read_text()
    assert "run.py studio" in shell.read_text()
