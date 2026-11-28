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
from runtime.config.storage import volume_key
from runtime.utils.files import read_json
from orchestration.protocol import APP_NAME, PROVISION_REGISTRY, state_path
from orchestration.deployment_state import DeploymentState, source_version, model_source_version, classify_error
from orchestration.leases import Lease, LeaseBusy, owner_stopped, terminal_proof

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
    def __init__(self, backend, *, prepare_enabled=True):
        self.root = ROOT
        self.backend = backend
        self.prepare_enabled = prepare_enabled
        # A generation worker consumes published deployments; only explicit
        # preparation needs to fingerprint local source files.
        if prepare_enabled:
            self.version = source_version()
            self.paths = model_paths()
            self.model_versions = {name: model_source_version(read_json(path))
                                   for name, path in self.paths.items()}
        self.registry = backend.modal.Dict.from_name(REGISTRY, create_if_missing=True)
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.verified = {}
        self.probe_revisions = {}
        self.closing = threading.Event()
        self.futures = {}
        self.wait_diagnostics = {}
        self.lock = threading.Lock()

    def version_for(self, spec):
        if spec is not None and hasattr(self, "model_versions"):
            return self.model_versions[spec["service"]]
        return self.version

    def assert_sources_unchanged(self, spec, expected):
        """Fail before a costly deployment if the worker's inputs changed."""
        current = (model_source_version(spec, root=self.root) if spec is not None and hasattr(self, "model_versions")
                   else source_version(root=self.root))
        if current != expected:
            raise RuntimeError("Sources changed during preparation; restart worker")

    def published_key(self, spec=None):
        """Select a published, compatible deployment without preparing or deploying."""
        service = spec["service"] if spec is not None else "orchestration"
        app = APP_NAME if spec is None else spec.get("app_name", "modal-model-" + service)
        # Existing batch reservations keep their pinned version. New batches
        # should not select an old deployment while an explicit rollout drains it.
        if self.registry.get("switching:" + app):
            return None
        key = self.registry.get("active:" + app)
        if key is None:
            # Older deployments did not publish an orchestration pointer.
            # A local record is only a locator, never readiness proof.
            path = self.root / ".runtime/models" / (service + ".json")
            try:
                key = read_json(path).get("key")
            except FileNotFoundError:
                return None
        if not key:
            return None
        record = self.registry.get(key, {})
        if (record.get("status") != "ready" or not record.get("version")
                or not record.get("app_revision")):
            return None
        if spec is not None and (record.get("spec_hash") != intent_digest(spec)
                                 or not record.get("cache_fingerprint")):
            return None
        return key

    def start(self, spec=None, *, retry=False, batch=None):
        if not getattr(self, "prepare_enabled", True):
            if retry:
                raise RuntimeError("Use the explicit prepare command to retry deployment")
            return self.published_key(spec)
        key = key_for(spec, self.version_for(spec))
        with self.lock:
            persisted = self.registry.get(key, {})
            future = self.futures.get(key)
            if not retry and persisted.get("status") == "failed":
                # A verification timeout is not proof that a rolling deployment
                # failed. Reconcile once per batch, without another deployment.
                stamp = (persisted.get("updated_at"), persisted.get("expected_version"))
                token = ("rollout", key, batch)
                if (persisted.get("error_code") == "deployment_verification"
                        and (future is None or future.done())
                        and self.verified.get(token) != stamp):
                    if self.reconcile_deployment(spec, key):
                        ready = self.registry[key]
                        self.verified[key, batch] = (ready["updated_at"], ready["version"])
                    self.verified[token] = stamp
                return key
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
                    # The source/cache hash may be republished as another Modal
                    # revision. Keep new submissions pinned to the verified one.
                    revision = self.probe_revision(app, persisted["version"])
                    if revision is not None and revision != persisted.get("app_revision"):
                        self.publish(key, {"app_revision": revision}, spec)
                        updated = self.registry[key]
                        stamp = (updated["updated_at"], updated["version"])
                    verified[key, batch] = stamp
                    if spec is None:
                        self.registry.put("active:" + app, key, skip_if_exists=True)
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
        prepare_enabled = getattr(self, "prepare_enabled", True)
        if pinned is None:
            if prepare_enabled:
                proposed = {service: key_for(binding["spec"], self.version_for(binding["spec"]))
                            for service, binding in services.items()}
            else:
                proposed = {service: self.published_key(binding["spec"])
                            for service, binding in services.items()}
                if any(key is None for key in proposed.values()):
                    return None  # Do not pin an unavailable deployment.
            if self.registry.put(name, proposed, skip_if_exists=True):
                pinned = proposed
            else:
                pinned = self.registry[name]
        for service, binding in services.items():
            spec = binding["spec"]
            if prepare_enabled and pinned[service] == key_for(spec, self.version_for(spec)):
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

    @staticmethod
    def direct_proof_key(key, identity):
        return "direct-proof:" + key + ":" + identity

    def record_direct(self, key, identity, job, record):
        """Reserve immutable intent; never overwrite another worker's accepted call."""
        name = self.direct_proof_key(key, identity)
        value = {"job": job, **record}
        if record["status"] in {"completed", "failed"} and (record.get("call_id") or record.get("result")):
            self.registry[name] = {"job": job, "status": record["status"]}
            return True
        if self.registry.put(name, value, skip_if_exists=True):
            return True
        existing = self.registry.get(name, {})
        if record["status"] == "submitting" and existing != value:
            raise RuntimeError("Direct submission already finalized or intent changed")
        return existing == value

    def direct_drained(self, key, identity, jobs):
        from runtime.tasks.job_client import check_record, remote_record
        proof = self.registry.get(self.direct_proof_key(key, identity), {})
        if len(jobs) != 1 or proof.get("job") != jobs[0]:
            return False
        if proof.get("status") in {"completed", "failed"}:
            return True
        if not all(proof.get(field) for field in ("prefix", "id", "intent_hash", "execution_hash")):
            return False
        result = remote_record(self.backend.volume, proof["prefix"], proof["id"])
        if result is None:
            return False
        check_record(proof, result)
        return result.get("status") in {"completed", "failed", "cancelled"}

    def drained(self, key):
        name = "admitted:" + key
        remaining = self.registry.get(name, {})
        for identity, jobs in list(remaining.items()):
            if identity.startswith("direct:"):
                if self.direct_drained(key, identity, jobs):
                    del remaining[identity]
                continue
            try:
                record = json.loads(b"".join(self.backend.volume.read_file(volume_key("/" + state_path(identity)))))
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
        # A crash may prevent release() from writing worker_finished. An exact
        # deployed-version probe plus a proven stopped owner is sufficient to
        # recover a completed rollout, but not an unconfirmed deployment.
        if operation.get("kind") == "deploy" and owner_stopped(owner, heartbeat, self.call_terminal):
            try:
                return bool(operation.get("app") and operation.get("version") and
                            self.deployed(operation["app"], operation["version"]))
            except Exception:
                return False
        return False

    def deployed_version(self, app, expected=None):
        from runtime.serving.deployment_probe import probe_name
        modal = self.backend.modal
        # The current App manifest is checked before starting any CPU probe.
        # A missing hash-specific tag is a mismatch, not a reason to warm
        # the previous deployment's stable probe. The tag isolates deployments
        # without requiring a paid workspace version retention window.
        tag = probe_name(expected)
        call = None
        try:
            info = modal.App.lookup(app).info()
            revision = info.lifecycle.version
            if not revision or tag not in info.functions:
                return None
            call = modal.Function.from_name(app, tag).spawn()
            actual = call.get(timeout=35)
            # A rollback/rollout racing this probe must not certify an old app.
            current = modal.App.lookup(app).info()
            if (current.lifecycle.version != revision
                    or current.functions.get(tag) != info.functions[tag]):
                return None
            if actual == expected and expected is not None:
                if not hasattr(self, "probe_revisions"):
                    self.probe_revisions = {}
                self.probe_revisions[app, expected] = revision
            return actual
        except modal.exception.NotFoundError:
            return None
        except TimeoutError:
            if call is not None:
                try:
                    call.cancel()
                except Exception:
                    pass
            return None

    def probe_revision(self, app, version):
        return getattr(self, "probe_revisions", {}).get((app, version))

    def deployed(self, app, version):
        return self.deployed_version(app, version) == version

    def wait_deployed(self, app, version, *, timeout=180, interval=3):
        """A rolling deployment may resolve the previous version briefly."""
        deadline = time.monotonic() + timeout
        while True:
            if self.deployed(app, version):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(interval)

    def reconcile_deployment(self, spec, key):
        """Promote an exactly verified late rollout; never download, deploy or spawn."""
        if key != key_for(spec, self.version_for(spec)):
            return False
        app = APP_NAME if spec is None else spec.get("app_name", "modal-model-" + spec["service"])
        try:
            with Lease(self.registry, "lock:" + app, key=key, proof=self.lease_proof) as lease:
                previous = self.registry.get(key, {})
                if previous.get("status") == "ready":
                    return True
                if (previous.get("status") != "failed"
                        or previous.get("error_code") != "deployment_verification"):
                    return False
                base = self.version_for(spec)
                self.assert_sources_unchanged(spec, base)
                try:
                    fingerprint = (remote_cache_fingerprint(spec, self.backend.volume)
                                   if spec is not None else None)
                except RuntimeError as exc:
                    if str(exc).startswith(("Remote cache manifest missing", "Remote cache differs")):
                        return False
                    raise
                version = intent_digest([base, spec, fingerprint]) if spec is not None else base
                if version != previous.get("expected_version") or not self.deployed(app, version):
                    return False
                self.assert_sources_unchanged(spec, base)
                with ExitStack() as gates:
                    if spec is not None:
                        gate = gates.enter_context(Lease(self.registry, "admission:" + app, key=key,
                                                         proof=self.lease_proof))
                        if self.registry.get("switching:" + app) != key:
                            return False
                        active = self.registry.get("active:" + app)
                        if active and active != key and not self.drained(active):
                            return False
                        gate.assert_owned()
                    lease.assert_owned()
                    if spec is not None:
                        self.registry["active:" + app] = key
                    self.publish(key, {"status": "ready", "version": version, "actual_version": version,
                                       "app_revision": self.probe_revision(app, version),
                                       "error": None, "error_code": None, "retryable": None,
                                       "last_failed_stage": None, "failed_stage": None,
                                       "spec_hash": intent_digest(spec) if spec is not None else None,
                                       "cache_fingerprint": fingerprint, "recovered_after_rollout": True}, spec)
                    if spec is not None:
                        self.registry.pop("switching:" + app, None)
                    else:
                        self.registry["active:" + app] = key
                    return True
        except LeaseBusy:
            return False

    def prepare(self, spec, key):
        # Atomic shared claim prevents two hosts mutating the same app/cache.
        app = APP_NAME if spec is None else spec.get("app_name", "modal-model-" + spec["service"])
        claim = "lock:" + app
        lease = Lease(self.registry, claim, key=key, proof=self.lease_proof)
        while True:
            try:
                lease.acquire().start_heartbeat()
                getattr(self, "wait_diagnostics", {}).pop(key, None)
                break
            except LeaseBusy:
                owner = self.registry.get(claim, {})
                heartbeat = (self.registry.get(Lease.heartbeat_key(claim, owner), {})
                             if owner.get("owner") else {})
                expired = heartbeat.get("expires_at", owner.get("acquired_at", time.time()) + 90) <= time.time()
                if expired or heartbeat.get("worker_finished"):
                    detail = "Expired deployment lease; remote outcome unconfirmed; inspect before retry"
                else:
                    detail = "Waiting for another model preparation owner"
                getattr(self, "wait_diagnostics", {})[key] = detail
                if not owner.get("owner") or not owner.get("key") or owner.get("key") == key:
                    return
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
                    self.publish(key, {"expected_version": version}, spec)
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
                    if deployment_requested:
                        with self.phase(key, spec, "deployment_verify"):
                            if not self.wait_deployed(app, version):
                                # Do not replay a deployment whose rollout is uncertain.
                                try:
                                    actual = self.deployed_version(app, version)
                                except Exception as exc:
                                    actual = f"unavailable ({type(exc).__name__})"
                                if actual != version:
                                    # Stable probe is diagnostic only; it cannot
                                    # certify the managed version after a timeout.
                                    if actual is None:
                                        try:
                                            actual = self.deployed_version(app)
                                        except Exception as exc:
                                            actual = f"unavailable ({type(exc).__name__})"
                                    self.publish(key, {"actual_version": actual}, spec)
                                    raise RuntimeError(
                                        f"Deployment version verification failed after rollout wait: "
                                        f"app={app}, expected={version}, actual={actual!r}")
                            self.publish(key, {"actual_version": version}, spec)
                    self.assert_sources_unchanged(spec, base_version)
                    if spec is not None:
                        self.registry["active:" + app] = key
                    self.publish(key, {"status": "ready", "version": version, "error": None,
                                       "app_revision": self.probe_revision(app, version),
                                       "last_failed_stage": None, "actual_version": version,
                                       "spec_hash": intent_digest(spec) if spec is not None else None,
                                       "cache_fingerprint": fingerprint if spec is not None else None}, spec)
                    if spec is None:
                        self.registry["active:" + app] = key
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
