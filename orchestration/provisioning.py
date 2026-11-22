"""Bounded, shared model preparation. Never runs inside a GPU container."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from runtime.config.model_spec import ROOT, validate
from runtime.tasks.task_identity import intent_digest
from runtime.tasks.job_client import remote_cache_fingerprint
from runtime.utils.files import read_json
from orchestration.protocol import APP_NAME, PROVISION_REGISTRY, state_path
from orchestration.deployment_state import DeploymentState, source_version, model_source_version, classify_error
from orchestration.leases import Lease, LeaseBusy, terminal_proof

REGISTRY = PROVISION_REGISTRY

def model_paths(root=ROOT):
    result = {}
    for path in sorted(root.glob("models/*/model.json")):
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
        self.verified = {}
        self.closing = threading.Event()
        self.futures = {}
        self.lock = threading.Lock()

    def version_for(self, spec):
        if spec is not None and hasattr(self, "model_versions"):
            return self.model_versions[spec["service"]]
        return self.version

    def assert_sources_unchanged(self, spec, expected):
        """Fail before a costly deployment if the worker's inputs changed."""
        current = (model_source_version(spec) if spec is not None and hasattr(self, "model_versions")
                   else source_version())
        if current != expected:
            raise RuntimeError("Sources changed during preparation; restart worker")

    def start(self, spec=None, *, retry=False, batch=None):
        key = key_for(spec, self.version_for(spec))
        with self.lock:
            persisted = self.registry.get(key, {})
            if not retry and persisted.get("status") == "failed":
                # Respect terminal failure even after a worker restart.
                return key
            future = self.futures.get(key)
            if not retry and persisted.get("status") == "ready" and (future is None or future.done()):
                verified = self.verified
                stamp = (persisted.get("updated_at"), persisted.get("version"))
                if verified.get((key, batch)) == stamp:
                    return key
                try:
                    cache_matches = (spec is None or remote_cache_fingerprint(spec, self.backend.volume)
                                     == persisted.get("cache_fingerprint"))
                except RuntimeError as exc:
                    if not str(exc).startswith(("Remote cache manifest missing", "Remote cache differs")):
                        raise
                    cache_matches = False
                app = APP_NAME if spec is None else spec.get("app_name", "modal-model-" + spec["service"])
                if cache_matches and persisted.get("version") and self.deployed(app, persisted["version"]):
                    verified[key, batch] = stamp
                    return key
                if not self.registry.get("admitted:" + key):
                    self.publish(key, {"status": "preparing", "reason": "readiness_changed"}, spec)
                future = None
            # Failure is sticky by default: worker polling must not cause an
            # infinite deployment/download loop. Only an explicit retry restarts it.
            needs_work = persisted.get("status") not in {"ready", "failed"} or retry
            if future is None or (future.done() and needs_work):
                self.futures[key] = self.pool.submit(self.prepare, spec, key)
        return key

    def batch_keys(self, services, batch):
        name = "batch:" + batch
        pinned = self.registry.get(name)
        if pinned is None:
            proposed = {service: key_for(binding["spec"], self.version_for(binding["spec"]))
                        for service, binding in services.items()}
            if self.registry.put(name, proposed, skip_if_exists=True):
                pinned = proposed
            else:
                pinned = self.registry[name]
        for service, binding in services.items():
            spec = binding["spec"]
            if pinned[service] == key_for(spec, self.version_for(spec)):
                self.start(spec, batch=batch)
            elif not self.registry.get(pinned[service]):
                raise RuntimeError("Original preparation version unavailable for batch " + batch)
        return pinned

    def admit_batch(self, services, preparation, groups, identity):
        """Reserve outstanding shots together, so a waiting rollout cannot split a batch."""
        # Existing reservations remain valid until their own terminal checkpoints.
        if all(set(members[identity]).issubset(self.registry.get("admitted:" + preparation[name], {}).get(identity, []))
               for name, members in groups.items() if identity in members):
            return True
        try:
            with ExitStack() as locks:
                for service in sorted(groups):
                    spec = services[service]["spec"]
                    app = spec.get("app_name", "modal-model-" + service)
                    key = preparation[service]
                    locks.enter_context(Lease(self.registry, "admission:" + app, key=key, proof=self.lease_proof))
                    status = self.registry.get(key, {}).get("status")
                    switching = self.registry.get("switching:" + app)
                    if switching and switching != key:
                        return False
                    active = self.registry.get("active:" + app)
                    if switching == key and active == key:
                        return False
                    if status == "ready" and not switching:
                        if active and active != key:
                            raise RuntimeError("Model deployment has been superseded: " + app)
                        if active is None:
                            prior = self.legacy_admissions(app)
                            prior.update(self.registry.get("admitted:" + key, {}))
                            self.registry["admitted:" + key] = prior
                            self.registry["active:" + app] = key
                for service, members in groups.items():
                    name = "admitted:" + preparation[service]
                    admitted = self.registry.get(name, {})
                    if admitted and admitted.keys().isdisjoint(members):
                        self.drained(preparation[service])
                        admitted = self.registry.get(name, {})
                    for shot, steps in members.items():
                        admitted[shot] = sorted(set(admitted.get(shot, [])) | set(steps))
                    self.registry[name] = admitted
                return True
        except LeaseBusy:
            return False

    def legacy_admissions(self, app):
        """Import receipts from this workspace on the first managed rollout."""
        entries = {}
        for path in (self.root / ".runtime/tasks").glob("*/*/plan.json"):
            plan = read_json(path)
            services = {name for name, binding in plan["services"].items()
                        if binding["spec"].get("app_name", "modal-model-" + name) == app}
            if not services:
                continue
            current = read_json(path.with_name("state.json"))
            for job in plan["jobs"]:
                if job["uses"].rsplit(".", 1)[0] not in services:
                    continue
                receipt = current.get("cloud_runs", {}).get(job["item"])
                record = current["jobs"][job["key"]]
                if receipt and "provisioning" not in receipt and record["status"] not in {"completed", "failed"}:
                    if record.get("call_id") and record["status"] == "uncertain":
                        try:
                            self.backend.modal.FunctionCall.from_id(record["call_id"]).get(timeout=0)
                        except RuntimeError as exc:
                            if "Local configuration differs from deployed model.json" in str(exc):
                                continue
                        except TimeoutError:
                            pass
                    if receipt.get("status") == "uncertain" and not record.get("call_id") and receipt.get("call_id"):
                        try:
                            final = self.backend.modal.FunctionCall.from_id(receipt["call_id"]).get(timeout=0)
                        except TimeoutError:
                            pass
                        else:
                            step = final["jobs"][job["key"]]
                            if step["status"] in {"pending", "blocked"} and not step.get("call_id"):
                                continue
                    entries.setdefault(receipt["id"], []).append(job["key"])
        return entries

    def drained(self, key):
        name = "admitted:" + key
        remaining = self.registry.get(name, {})
        for identity, jobs in list(remaining.items()):
            try:
                record = json.loads(b"".join(self.backend.volume.read_file("/" + state_path(identity))))
            except FileNotFoundError:
                continue
            if all(record["jobs"].get(job, {}).get("status") in {"completed", "failed"} for job in jobs):
                del remaining[identity]
        if remaining:
            self.registry[name] = remaining
        else:
            self.registry.pop(name, None)
        return not remaining

    def command(self, action, entry, **environment):
        workspace = getattr(self, "root", ROOT)
        argv = [sys.executable, "-m", "modal", action, entry]
        if action == "deploy":
            # Keep previously accepted inference calls alive while deploying.
            # Recreate terminates active GPU containers and loses their results.
            argv.extend(["--strategy", "rolling"])
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

    def command_benchmark(self, model_path):
        """Use the same public HF downloader as the standalone CLI."""
        workspace = getattr(self, "root", ROOT)
        subprocess.run([sys.executable, str(workspace / "tools/download_models.py"),
                        str(model_path), "--execute"], cwd=workspace, check=True,
                       timeout=90000)

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

    def wait_deployed(self, app, version, *, timeout=90, interval=3):
        """A rolling deployment may resolve the previous version briefly."""
        deadline = time.monotonic() + timeout
        while True:
            if self.deployed(app, version):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(interval)

    def prepare(self, spec, key):
        # Atomic shared claim prevents two hosts mutating the same app/cache.
        app = APP_NAME if spec is None else spec.get("app_name", "modal-model-" + spec["service"])
        claim = "lock:" + app
        lease = Lease(self.registry, claim, key=key, proof=self.lease_proof)
        while True:
            try:
                lease.acquire().start_heartbeat()
                break
            except LeaseBusy:
                owner = self.registry.get(claim, {})
                if not owner.get("owner") or not owner.get("key") or owner.get("key") == key:
                    return
                heartbeat = self.registry.get(Lease.heartbeat_key(claim, owner), {})
                if heartbeat.get("expires_at", 0) <= time.time() or self.closing.wait(1):
                    return
        deployment_requested = False
        try:
            base_version = self.version_for(spec)
            weights_prepared = False
            for attempt in range(3):
                try:
                    self.assert_sources_unchanged(spec, base_version)
                    previous = None
                    if spec is not None:
                        gate = Lease(self.registry, "admission:" + app, key=key, proof=self.lease_proof)
                        while True:
                            try:
                                with gate:
                                    previous = self.registry.get("active:" + app)
                                    self.registry["switching:" + app] = key
                                break
                            except LeaseBusy:
                                if self.closing.wait(0.25):
                                    return
                    if spec is not None and previous is None:
                        legacy = self.legacy_admissions(app)
                        if legacy:
                            previous = "legacy:" + app
                            self.registry["admitted:" + previous] = legacy
                    if previous and not deployment_requested:
                        if not self.drained(previous):
                            if previous != key:
                                self.publish(key, {"status": "waiting_for_drain"}, spec)
                            while not self.closing.wait(5):
                                if self.drained(previous):
                                    break
                            else:
                                return
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
                                # Benchmark owns HF downloads for every model, including
                                # Pika. Its specialized stage still uses SGLang materialization.
                                with self.phase(key, spec, "weights"):
                                    with lease.external("download"):
                                        self.command_benchmark(path)
                                result = {}
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
                    needs_deploy = not deployment_requested and not self.deployed(app, version)
                    if needs_deploy:
                        # Validate again after potentially slow cache checks,
                        # before a deployment can disrupt running generations.
                        self.assert_sources_unchanged(spec, base_version)
                        with self.phase(key, spec, "build_deploy"):
                            with lease.external("deploy", app=app, version=version):
                                self.command("deploy", "pipeline/deploy_shot.py" if spec is None else
                                             "orchestration/provision_entry.py", **env,
                                             MODEL_PREPARE="0", MODEL_DEPLOY_VERSION=version)
                                deployment_requested = True
                    if deployment_requested and not self.wait_deployed(app, version):
                        # Do not immediately redeploy the same version again:
                        # the rollout may still be settling or require review.
                        raise RuntimeError("Deployment version verification failed after rollout wait")
                    self.assert_sources_unchanged(spec, base_version)
                    if spec is not None:
                        self.registry["active:" + app] = key
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
            release_gate = not lease.operation and (not deployment_requested or self.registry.get(key, {}).get("status") == "ready")
            if spec is not None and release_gate and self.registry.get("switching:" + app) == key:
                self.registry.pop("switching:" + app, None)
            lease.release()

    def close(self):
        self.closing.set()
        self.pool.shutdown(wait=True)
