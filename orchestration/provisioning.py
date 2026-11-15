"""Bounded, shared model preparation. Never runs inside a GPU container."""
from concurrent.futures import ThreadPoolExecutor
import os
import subprocess
import sys
import threading
import time

from shared.config.model_spec import ROOT, validate
from shared.tasks.batch import intent_digest
from shared.tasks.job_client import remote_cache_fingerprint
from shared.utils.files import read_json, sha256_file
from orchestration.protocol import APP_NAME, PROVISION_REGISTRY

REGISTRY = PROVISION_REGISTRY


def source_version(root=ROOT):
    paths = sorted({*root.glob("shared/**/*.py"), *root.glob("orchestration/**/*.py"),
                    *root.glob("*/model.json")})
    return intent_digest([(p.relative_to(root).as_posix(), sha256_file(p)) for p in paths])


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
        self.backend = backend
        self.version = source_version()
        self.paths = model_paths()
        self.registry = backend.modal.Dict.from_name(REGISTRY, create_if_missing=True)
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.futures = {}
        self.lock = threading.Lock()

    def start(self, spec=None):
        key = key_for(spec, self.version)
        with self.lock:
            if key not in self.futures:
                self.futures[key] = self.pool.submit(self.prepare, spec, key)
        return key

    def command(self, action, entry, **environment):
        subprocess.run([sys.executable, "-m", "modal", action, entry], cwd=ROOT,
                       env={**os.environ, **environment}, check=True, timeout=90000)

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
        deadline = time.monotonic() + 90000
        while not self.registry.put(claim, key, skip_if_exists=True):
            if time.monotonic() >= deadline:
                self.registry[key] = {"status": "failed", "error": "Preparation lock still held: " + claim}
                return
            time.sleep(2)
        try:
            for attempt in range(3):
                try:
                    self.registry[key] = {"status": "preparing", "attempt": attempt + 1}
                    env = {}
                    if spec is not None:
                        path = self.paths[spec["service"]]
                        if validate(read_json(path)) != spec:
                            raise ValueError("Registered model configuration differs from local model.json")
                        env = {"MODEL_CONFIG": str(path)}
                        # Downloader performs full inventory verification and reuses valid weights.
                        self.command("run", "orchestration/provision_entry.py", **env, MODEL_PREPARE="1")
                        fingerprint = remote_cache_fingerprint(spec, self.backend.volume)
                        version = intent_digest([self.version, spec, fingerprint])
                    else:
                        version = self.version
                    if not self.deployed(app, version):
                        self.command("deploy", "orchestration/cloud_app.py" if spec is None else
                                     "orchestration/provision_entry.py", **env,
                                     MODEL_PREPARE="0", MODEL_DEPLOY_VERSION=version)
                    if not self.deployed(app, version):
                        raise RuntimeError("Deployment version verification failed")
                    if source_version() != self.version:
                        raise RuntimeError("Sources changed during preparation; restart worker")
                    self.registry[key] = {"status": "ready", "version": version}
                    return
                except Exception as exc:
                    if attempt == 2:
                        self.registry[key] = {"status": "failed", "error": str(exc)}
                        return
                    time.sleep(2 ** (attempt + 1))
        finally:
            self.registry.pop(claim)

    def close(self):
        self.pool.shutdown(wait=True)
