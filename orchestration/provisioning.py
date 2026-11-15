"""Bounded, shared model preparation. Never runs inside a GPU container."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
import subprocess
import sys
import threading
import time
from uuid import uuid4
from pathlib import Path

from shared.config.model_spec import ROOT, validate
from shared.tasks.batch import intent_digest
from shared.tasks.job_client import remote_cache_fingerprint
from shared.utils.files import read_json, sha256_file
from orchestration.protocol import APP_NAME, PROVISION_REGISTRY
from orchestration import state
from orchestration.deployment_state import DeploymentState, source_version, model_source_version, classify_error
from orchestration.leases import Lease, LeaseBusy, terminal_proof

REGISTRY = PROVISION_REGISTRY






def model_paths(root=ROOT):
    result = {}
    for path in sorted(root.glob("*/model.json")):
        spec = validate(read_json(path))
        if spec["service"] in result:
            raise ValueError("Duplicate model service: " + spec["service"])
        result[spec["service"]] = path
    return result


def key_for(spec, version):
    return intent_digest([spec, version])


class Provisioner:
    def __init__(self, backend):
        self.root = ROOT
        self.backend = backend
        self.version = source_version()
        self.paths = model_paths()
        self.model_versions = {name: model_source_version(read_json(path))
                               for name, path in self.paths.items()}
        self.registry = backend.modal.Dict.from_name(REGISTRY, create_if_missing=True)
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.futures = {}
        self.lock = threading.Lock()

    def version_for(self, spec):
        if spec is not None and hasattr(self, "model_versions"):
            return self.model_versions[spec["service"]]
        return self.version

    def start(self, spec=None, *, retry=False):
        key = key_for(spec, self.version_for(spec))
        with self.lock:
            persisted = self.registry.get(key, {})
            if not retry and persisted.get("status") == "failed":
                # Respect terminal failure even after a worker restart.
                return key
            future = self.futures.get(key)
            if spec is not None and persisted.get("status") == "ready" and (future is None or future.done()):
                try:
                    changed = remote_cache_fingerprint(spec, self.backend.volume) != persisted.get("cache_fingerprint")
                except RuntimeError:
                    changed = True
                if changed:
                    self.publish(key, {"status": "preparing", "reason": "cache_changed"}, spec)
                    future = None
            # Failure is sticky by default: worker polling must not cause an
            # infinite deployment/download loop. Only an explicit retry restarts it.
            if future is None or (retry and future.done()
                                  and persisted.get("status") == "failed"):
                self.futures[key] = self.pool.submit(self.prepare, spec, key)
        return key

    def command(self, action, entry, **environment):
        workspace = getattr(self, "root", ROOT)
        argv = [sys.executable, "-m", "modal", action, entry]
        if action == "deploy":
            argv.extend(["--strategy", "recreate"])
        service = "orchestration"
        if environment.get("MODEL_CONFIG"):
            service = read_json(Path(environment["MODEL_CONFIG"]))["service"]
        logs = workspace / ".runtime/logs/models"
        logs.mkdir(parents=True, exist_ok=True)
        path = logs / (service + ".log")
        if path.exists() and path.stat().st_size > 8 * 1024 * 1024:
            path.replace(path.with_suffix(".previous.log"))
        with path.open("a", encoding="utf-8") as stream:
            subprocess.run(argv, cwd=workspace, stdout=stream, stderr=subprocess.STDOUT,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", **environment},
                           check=True, timeout=90000)

    def store(self):
        if not hasattr(self, "_state_store"):
            self._state_store = DeploymentState(getattr(self, "root", ROOT), self.registry)
        return self._state_store

    def publish(self, key, value, spec):
        return self.store().publish(key, value, spec)

    def phase(self, key, spec, stage):
        return self.store().phase(key, spec, stage)

    def call_terminal(self, call_id):
        try:
            self.backend.modal.FunctionCall.from_id(call_id).get(timeout=0)
            return True
        except Exception as exc:
            # Only explicit terminal platform failures prove termination. Network
            # failures, expired receipts and generic user exceptions do not.
            return type(exc).__name__ in {"FunctionTimeoutError", "InternalFailure"}

    def lease_proof(self, owner, heartbeat):
        if terminal_proof(owner, heartbeat, self.call_terminal):
            return True
        operation = heartbeat.get("operation") or {}
        if heartbeat.get("worker_finished") and operation.get("kind") == "deploy":
            try:
                return bool(operation.get("app") and operation.get("version") and
                            self.deployed(operation["app"], operation["version"]))
            except Exception:
                return False
        return False

    def deployed(self, app, version):
        try:
            info = self.backend.modal.Function.from_name(app, "provision_info").remote()
        except self.backend.modal.exception.NotFoundError:
            return False
        return info == version

    def prepare(self, spec, key):
        # Atomic shared claim prevents two hosts mutating the same app/cache.
        app = APP_NAME if spec is None else spec.get("app_name", "modal-model-" + spec["service"])
        claim = "lock:" + app
        lease = Lease(self.registry, claim, key=key, proof=self.lease_proof)
        try:
            lease.acquire().start_heartbeat()
        except LeaseBusy as exc:
            self.publish(key, {"status": "failed", "error_code": "lease",
                               "error": "Stale preparation lock or active owner: " + str(exc), "retryable": False}, spec)
            return
        try:
            base_version = self.version_for(spec)
            downloader_deployed = False
            weights_prepared = False
            for attempt in range(3):
                try:
                    self.publish(key, {"status": "preparing", "attempt": attempt + 1}, spec)
                    env = {}
                    if spec is not None:
                        path = self.paths[spec["service"]]
                        if validate(read_json(path)) != spec:
                            raise ValueError("Registered model configuration differs from local model.json")
                        env = {"MODEL_CONFIG": str(path)}
                        if not weights_prepared:
                            # A matching persisted manifest is a cheap cache hit.
                            # Only missing/stale caches need a CPU preparer deployment.
                            try:
                                with self.phase(key, spec, "cache_check"):
                                    fingerprint = remote_cache_fingerprint(spec, self.backend.volume)
                            except RuntimeError as exc:
                                if not str(exc).startswith(("Remote cache manifest missing", "Remote cache differs")):
                                    raise
                                if not downloader_deployed:
                                    with self.phase(key, spec, "prepare_service"):
                                        with lease.external("deploy", app="prepare-" + spec["service"], version=base_version):
                                            self.command("deploy", "orchestration/provision_entry.py", **env,
                                                         MODEL_PREPARE="1", MODEL_DEPLOY_VERSION=base_version)
                                    downloader_deployed = True
                                with self.phase(key, spec, "weights"):
                                    download = self.backend.modal.Function.from_name(
                                        "prepare-" + spec["service"], "download")
                                    with lease.external("download"):
                                        call = download.spawn(repair=True)
                                        lease.attach_call(call.object_id)
                                        result = call.get(timeout=86500)
                                if isinstance(result, dict):
                                    self.publish(key, {"status": "deploying",
                                                       "cache_fingerprint": result.get("fingerprint")}, spec)
                                with self.phase(key, spec, "cache_check"):
                                    fingerprint = remote_cache_fingerprint(spec, self.backend.volume)
                            weights_prepared = True
                        else:
                            with self.phase(key, spec, "cache_check"):
                                fingerprint = remote_cache_fingerprint(spec, self.backend.volume)
                        version = intent_digest([base_version, spec, fingerprint])
                    else:
                        version = base_version
                    if not self.deployed(app, version):
                        with self.phase(key, spec, "build_deploy"):
                            with lease.external("deploy", app=app, version=version):
                                self.command("deploy", "orchestration/cloud_app.py" if spec is None else
                                             "orchestration/provision_entry.py", **env,
                                             MODEL_PREPARE="0", MODEL_DEPLOY_VERSION=version)
                    if not self.deployed(app, version):
                        raise RuntimeError("Deployment version verification failed")
                    current_source = (model_source_version(spec) if spec is not None and hasattr(self, "model_versions")
                                      else source_version())
                    if current_source != base_version:
                        raise RuntimeError("Sources changed during preparation; restart worker")
                    self.publish(key, {"status": "ready", "version": version, "error": None,
                                       "last_failed_stage": None,
                                       "spec_hash": intent_digest(spec) if spec is not None else None,
                                       "cache_fingerprint": fingerprint if spec is not None else None}, spec)
                    return
                except Exception as exc:
                    print(f"Preparation {app}, attempt {attempt + 1}: {type(exc).__name__}: {exc}", flush=True)
                    info = classify_error(exc, self.registry.get(key, {}).get("last_failed_stage"))
                    if lease.operation:
                        if lease.operation.get("call_id") and self.call_terminal(lease.operation["call_id"]):
                            lease.operation = None
                            lease.renew()
                        else:
                            info.update(error_code="uncertain_external", retryable=False)
                    if attempt == 2 or not info["retryable"]:
                        self.publish(key, {"status": "failed", "error": str(exc), **info}, spec)
                        return
                    time.sleep(2 ** (attempt + 1))
        finally:
            lease.release()

    def close(self):
        self.pool.shutdown(wait=True)
