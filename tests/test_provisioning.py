import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from orchestration.provisioning import Provisioner, key_for, model_paths, model_source_version, source_version
from orchestration.deployment_state import classify_error
from orchestration.leases import Lease
from runtime.config.model_spec import validate
from runtime.utils.files import atomic_json
from tests.test_model_service import SPEC


class Registry(dict):
    def put(self, key, value, skip_if_exists=False):
        if skip_if_exists and key in self:
            return False
        self[key] = value
        return True


class ProvisioningTests(unittest.TestCase):
    def test_configuration_only_discovery_and_duplicates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            atomic_json(root / "models/new/model.json", SPEC)
            self.assertEqual(list(model_paths(root)), ["example"])
            before = source_version(root)
            atomic_json(root / "models/new/model.json", {**SPEC, "revision": "b" * 40})
            self.assertNotEqual(before, source_version(root))
            atomic_json(root / "models/duplicate/model.json", SPEC)
            with self.assertRaises(ValueError):
                model_paths(root)

    def provisioner(self, path):
        p = Provisioner.__new__(Provisioner)
        p.root = path.parent
        p.verified = {}
        p.closing = threading.Event()
        p.backend = Mock()
        p.registry = Registry()
        p.version = "source"
        p.paths = {"example": path}
        p.command = Mock()
        p.command_benchmark = Mock()
        p.deployed = Mock(side_effect=[False, True])
        return p

    def test_valid_persisted_cache_skips_cpu_preparation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "model.json"
            atomic_json(path, SPEC)
            p = self.provisioner(path)
            with patch("orchestration.provisioning.remote_cache_fingerprint", return_value="weights"), \
                 patch("orchestration.provisioning.source_version", return_value="source"):
                p.prepare(validate(SPEC), "key")
            self.assertEqual([c.args[0] for c in p.command.call_args_list], ["deploy"])
            p.backend.modal.Function.from_name.assert_not_called()
            self.assertEqual(p.registry["key"]["status"], "ready")
            self.assertFalse(any(k.startswith("lock:") for k in p.registry))

    def test_completed_lease_cleans_up_heartbeat_but_unknown_external_work_remains_claimed(self):
        registry = Registry()
        complete = Lease(registry, "lock:complete")
        complete.acquire()
        heartbeat = complete.heartbeat_key(complete.name, complete.owner)
        self.assertIn(heartbeat, registry)
        complete.release()
        self.assertNotIn("lock:complete", registry)
        self.assertNotIn(heartbeat, registry)

        uncertain = Lease(registry, "lock:uncertain")
        uncertain.acquire()
        with self.assertRaisesRegex(RuntimeError, "connection dropped"):
            with uncertain.external("deploy", app="model", version="v1"):
                raise RuntimeError("connection dropped")
        uncertain.release()
        self.assertEqual(registry["lock:uncertain"], uncertain.owner)
        uncertain_heartbeat = registry[uncertain.heartbeat_key(uncertain.name, uncertain.owner)]
        self.assertTrue(uncertain_heartbeat["worker_finished"])
        self.assertEqual(uncertain_heartbeat["operation"]["kind"], "deploy")

    def test_uncertain_deployment_fails_closed_without_replaying_side_effect(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "model.json"
            atomic_json(path, SPEC)
            p = self.provisioner(path)
            p.command_benchmark.side_effect = RuntimeError("download failed")
            with patch("orchestration.provisioning.time.sleep"), \
                 patch("orchestration.provisioning.source_version", return_value="source"), \
                 patch("orchestration.provisioning.remote_cache_fingerprint",
                       side_effect=RuntimeError("Remote cache manifest missing")):
                p.prepare(validate(SPEC), "key")
            p.command_benchmark.assert_called_once_with(path)
            p.deployed.assert_not_called()
            self.assertEqual(p.registry["key"]["status"], "failed")
            self.assertEqual([s["stage"] for s in p.registry["key"]["stages"]],
                             ["cache_check", "weights"])
            self.assertTrue(all(stage["status"] == "failed" and stage["duration_ms"] >= 0 for stage in p.registry["key"]["stages"]))

    def test_command_writes_model_log_in_runtime_without_cloud_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "model.json"
            atomic_json(path, SPEC)
            p = self.provisioner(path)
            with patch("orchestration.provisioning.subprocess.run") as run:
                Provisioner.command(p, "deploy", "orchestration/provision_entry.py", MODEL_CONFIG=str(path))
            self.assertIn("--strategy", run.call_args.args[0])
            self.assertEqual(run.call_args.kwargs["cwd"], Path(temp))
            self.assertTrue((Path(temp) / ".runtime/logs/models/example.log").is_file())

    def test_matching_deployment_is_reused_after_cache_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "model.json"
            atomic_json(path, SPEC)
            p = self.provisioner(path)
            p.deployed = Mock(return_value=True)
            with patch("orchestration.provisioning.remote_cache_fingerprint", return_value="weights"), \
                 patch("orchestration.provisioning.source_version", return_value="source"):
                p.prepare(validate(SPEC), "key")
            p.command.assert_not_called()
            self.assertEqual(p.registry["key"]["status"], "ready")

    def test_rolling_deployment_waits_for_version_convergence(self):
        p = self.provisioner(Path("unused.json"))
        p.deployed = Mock(side_effect=[False, False, True])
        with patch("orchestration.provisioning.time.sleep") as sleep:
            self.assertTrue(p.wait_deployed("model", "v2", timeout=15, interval=1))
        self.assertEqual(p.deployed.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_rollout_mismatch_records_expected_actual_without_redeploy(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "model.json"
            atomic_json(path, SPEC)
            p = self.provisioner(path)
            p.deployed = Mock(return_value=False)
            p.wait_deployed = Mock(return_value=False)
            p.deployed_version = Mock(return_value="old-version")
            with patch("orchestration.provisioning.source_version", return_value="source"), \
                 patch("orchestration.provisioning.remote_cache_fingerprint", return_value="weights"):
                p.prepare(validate(SPEC), "key")
            record = p.registry["key"]
            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["actual_version"], "old-version")
            self.assertIn(record["expected_version"], record["error"])
            self.assertIn("old-version", record["error"])
            self.assertEqual(record["last_failed_stage"], "deployment_verify")
            self.assertEqual(record["error_code"], "deployment_verification")
            self.assertFalse(record["retryable"])
            p.command.assert_called_once()

    def test_final_rollout_probe_can_confirm_exact_version(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "model.json"
            atomic_json(path, SPEC)
            p = self.provisioner(path)
            p.deployed = Mock(return_value=False)
            p.wait_deployed = Mock(return_value=False)
            p.deployed_version = Mock(side_effect=lambda app: p.registry["key"]["expected_version"])
            with patch("orchestration.provisioning.source_version", return_value="source"), \
                 patch("orchestration.provisioning.remote_cache_fingerprint", return_value="weights"):
                p.prepare(validate(SPEC), "key")
            self.assertEqual(p.registry["key"]["status"], "ready")
            self.assertEqual(p.registry["key"]["expected_version"], p.registry["key"]["actual_version"])
            p.command.assert_called_once()

    def test_version_verification_failure_is_not_redeployed_blindly(self):
        result = classify_error(RuntimeError("Deployment version verification failed after rollout wait"))
        self.assertEqual(result["error_code"], "deployment_verification")
        self.assertFalse(result["retryable"])

    def test_changed_sources_fail_before_any_cloud_deployment(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "model.json"
            atomic_json(path, SPEC)
            p = self.provisioner(path)
            with patch("orchestration.provisioning.source_version", return_value="changed"), \
                 patch("orchestration.provisioning.remote_cache_fingerprint") as cache:
                p.prepare(validate(SPEC), "key")
            cache.assert_not_called()
            p.deployed.assert_not_called()
            p.command.assert_not_called()
            self.assertEqual(p.registry["key"]["status"], "failed")
            self.assertEqual(p.registry["key"]["error_code"], "configuration")

    def test_sources_changing_during_cache_check_cannot_trigger_redeploy(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "model.json"
            atomic_json(path, SPEC)
            p = self.provisioner(path)
            with patch("orchestration.provisioning.source_version", side_effect=["source", "changed"]), \
                 patch("orchestration.provisioning.remote_cache_fingerprint", return_value="weights"):
                p.prepare(validate(SPEC), "key")
            p.command.assert_not_called()
            self.assertEqual(p.registry["key"]["status"], "failed")

    def test_overlay_success_uses_shared_benchmark_once(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "model.json"
            overlay = {**SPEC, "cache": {"mode": "sglang_overlay"}}
            atomic_json(path, overlay)
            p = self.provisioner(path)
            calls = 0

            def cache_check(*_args):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("Remote cache manifest missing")
                return "weights"

            with patch("orchestration.provisioning.remote_cache_fingerprint", side_effect=cache_check), \
                 patch("orchestration.provisioning.source_version", return_value="source"):
                p.prepare(validate(overlay), "key")
            p.command_benchmark.assert_called_once_with(path)
            p.backend.modal.Function.from_name.assert_not_called()
            self.assertEqual(p.registry["key"]["status"], "ready")

    def test_overlay_uncertain_external_failure_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "model.json"
            overlay = {**SPEC, "cache": {"mode": "sglang_overlay"}}
            atomic_json(path, overlay)
            p = self.provisioner(path)
            p.command_benchmark.side_effect = RuntimeError("network")
            with patch("orchestration.provisioning.remote_cache_fingerprint",
                       side_effect=RuntimeError("Remote cache manifest missing")), \
                 patch("orchestration.provisioning.source_version", return_value="source"), \
                 patch("orchestration.provisioning.time.sleep"):
                p.prepare(validate(overlay), "key")
            p.command_benchmark.assert_called_once_with(path)
            self.assertEqual(p.registry["key"]["status"], "failed")

    def test_command_reads_model_config_and_runs_bounded_deploy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "example/model.json"
            atomic_json(config, SPEC)
            p = self.provisioner(config)
            with patch("orchestration.provisioning.subprocess.run") as run:
                Provisioner.command(p, "deploy", "orchestration/provision_entry.py", MODEL_CONFIG=str(config))
            args, kwargs = run.call_args
            self.assertEqual(args[0][-2:], ["--strategy", "rolling"])
            self.assertEqual(kwargs["timeout"], 90000)
            self.assertEqual(kwargs["env"]["MODEL_CONFIG"], str(config))
            self.assertTrue((p.root / ".runtime/logs/models/example.log").exists())

    def test_failure_is_sticky_until_explicit_retry(self):
        spec = validate(SPEC)
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "model.json"
            atomic_json(config, SPEC)
            p = self.provisioner(config)
            p.lock = threading.Lock()
            p.pool = Mock()
            key = key_for(spec, "source")
            future = Mock()
            future.done.return_value = True
            p.futures = {key: future}
            p.registry[key] = {"status": "failed"}
            self.assertEqual(p.start(spec), key)
            p.pool.submit.assert_not_called()
            self.assertEqual(p.start(spec, retry=True), key)
            p.pool.submit.assert_called_once_with(p.prepare, spec, key)

    def test_failure_survives_worker_restart_without_auto_redeploy(self):
        spec = validate(SPEC)
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "model.json"
            atomic_json(config, SPEC)
            p = self.provisioner(config)
            p.lock = threading.Lock()
            p.pool = Mock()
            p.futures = {}
            key = key_for(spec, "source")
            p.registry[key] = {"status": "failed", "error": "download failed"}
            self.assertEqual(p.start(spec), key)
            p.pool.submit.assert_not_called()
            self.assertEqual(p.start(spec, retry=True), key)
            p.pool.submit.assert_called_once_with(p.prepare, spec, key)

    def test_ready_cache_and_deployment_do_not_reschedule_preparation(self):
        spec = validate(SPEC)
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "model.json"
            atomic_json(config, SPEC)
            p = self.provisioner(config)
            p.lock, p.pool, p.futures = threading.Lock(), Mock(), {}
            p.deployed = Mock(return_value=True)
            key = key_for(spec, "source")
            p.registry[key] = {"status": "ready", "cache_fingerprint": "weights", "version": "v1"}
            with patch("orchestration.provisioning.remote_cache_fingerprint", return_value="weights"):
                self.assertEqual(p.start(spec), key)
                self.assertEqual(p.start(spec), key)
            p.pool.submit.assert_not_called()
            p.deployed.assert_called_once_with(spec.get("app_name", "modal-model-example"), "v1")

    def test_ready_orchestration_does_not_reschedule_preparation(self):
        p = self.provisioner(Path("unused.json"))
        p.lock, p.pool, p.futures = threading.Lock(), Mock(), {}
        p.deployed = Mock(return_value=True)
        key = key_for(None, "source")
        p.registry[key] = {"status": "ready", "version": "source"}
        self.assertEqual(p.start(), key)
        p.pool.submit.assert_not_called()

    def test_ready_record_without_deployment_requires_preparation(self):
        spec = validate(SPEC)
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "model.json"
            atomic_json(config, SPEC)
            p = self.provisioner(config)
            p.lock, p.pool, p.futures = threading.Lock(), Mock(), {}
            p.deployed = Mock(return_value=False)
            key = key_for(spec, "source")
            p.registry[key] = {"status": "ready", "cache_fingerprint": "weights", "version": "v1"}
            with patch("orchestration.provisioning.remote_cache_fingerprint", return_value="weights"):
                self.assertEqual(p.start(spec), key)
            self.assertEqual(p.registry[key]["status"], "preparing")
            p.pool.submit.assert_called_once_with(p.prepare, spec, key)

    def test_competing_preparer_cannot_overwrite_active_owner_status(self):
        spec = validate(SPEC)
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "model.json"
            atomic_json(config, SPEC)
            p = self.provisioner(config)
            key = key_for(spec, "source")
            preparing = {"status": "preparing", "attempt": 1}
            p.registry[key] = dict(preparing)
            with Lease(p.registry, "lock:modal-model-example"):
                p.prepare(spec, key)
                self.assertEqual(p.registry[key], preparing)
            p.command.assert_not_called()

    def test_stale_claim_fails_closed_without_deleting_other_owner(self):
        import time
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "model.json"
            atomic_json(config, SPEC)
            p = self.provisioner(config)
            name = "lock:" + SPEC.get("app_name", "modal-model-example")
            holder = {"key": "other", "owner": "dead", "acquired_at": time.time() - 100000}
            p.registry[name] = holder
            p.prepare(validate(SPEC), "my-key")
            self.assertEqual(p.registry[name], holder)
            self.assertNotIn("my-key", p.registry)
            p.command.assert_not_called()

    def test_model_source_versions_do_not_cross_redeploy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            moss, pika = {
                **SPEC, "service": "moss", "validator": "moss",
                "validator_module": "models.001-moss.model_impl.params",
            }, {
                **SPEC, "service": "pika", "validator": "pika",
                "validator_module": "models.002-pika.model_impl.params",
            }
            atomic_json(root / "models/001-moss/model.json", moss)
            atomic_json(root / "models/002-pika/model.json", pika)
            for name in ("runtime/serving/legacy_cache_preparer.py", "models/001-moss/model_impl/params.py",
                         "models/002-pika/model_impl/params.py", "pipeline/s01_build_plan.py"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("a", encoding="utf-8")
            moss_before, pika_before = model_source_version(moss, root), model_source_version(pika, root)
            (root / "models/001-moss/model_impl/params.py").write_text("b", encoding="utf-8")
            self.assertNotEqual(moss_before, model_source_version(moss, root))
            self.assertEqual(pika_before, model_source_version(pika, root))
            (root / "pipeline/s01_build_plan.py").write_text("b", encoding="utf-8")
            self.assertEqual(pika_before, model_source_version(pika, root))
            atomic_json(root / "models/002-pika/model.json", {**pika, "revision": "b" * 40})
            self.assertNotEqual(pika_before, model_source_version(pika, root))

    def test_new_rollout_waits_for_all_six_shots_including_unsubmitted(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "model.json"
            atomic_json(path, SPEC)
            spec = validate(SPEC)
            worker_a = self.provisioner(path)
            worker_b = self.provisioner(path)
            worker_b.registry = worker_a.registry
            worker_b.closing = threading.Event()
            worker_a.registry["old"] = {"status": "ready"}
            services = {"example": {"spec": spec}}
            members = {f"{shot:064x}": [f"image/{shot}"] for shot in range(6)}
            keys = {"example": "old"}
            self.assertTrue(worker_a.admit_batch(services, keys, {"example": members}, next(iter(members))))
            progress = {}
            def checkpoint(path):
                identity = path.rsplit("/", 2)[1]
                if identity not in progress:
                    raise FileNotFoundError
                return [json.dumps({"jobs": {members[identity][0]: {"status": progress[identity]}}}).encode()]
            worker_b.backend.volume.read_file.side_effect = checkpoint
            def finish_queue(_):
                worker_b.command.assert_not_called()
                for identity in members:
                    self.assertTrue(worker_a.admit_batch(services, keys, {"example": members}, identity))
                    progress[identity] = "completed"
                return False
            worker_b.closing.wait = Mock(side_effect=finish_queue)
            with patch("orchestration.provisioning.source_version", return_value="source"), \
                 patch("orchestration.provisioning.remote_cache_fingerprint", return_value="weights"):
                worker_b.prepare(spec, "new")
            worker_b.command.assert_called_once()
            self.assertEqual(worker_b.registry["new"]["status"], "ready")
            self.assertNotIn("admitted:old", worker_b.registry)

    def test_transient_cache_query_preserves_ready_record(self):
        p = self.provisioner(Path("unused.json"))
        p.lock, p.pool, p.futures = threading.Lock(), Mock(), {}
        spec = validate(SPEC)
        key = key_for(spec, "source")
        p.registry[key] = {"status": "ready", "version": "v1"}
        with patch("orchestration.provisioning.remote_cache_fingerprint", side_effect=RuntimeError("network unavailable")):
            with self.assertRaisesRegex(RuntimeError, "network unavailable"):
                p.start(spec)
        self.assertEqual(p.registry[key]["status"], "ready")
        p.pool.submit.assert_not_called()

    def test_second_worker_uses_persisted_batch_version_after_source_change(self):
        p = self.provisioner(Path("unused.json"))
        p.start = Mock()
        services = {"example": {"spec": validate(SPEC)}}
        original = p.batch_keys(services, "batch")
        p.registry[original["example"]] = {"status": "ready"}
        p.start.reset_mock()
        p.version = "next"
        self.assertEqual(p.batch_keys(services, "batch"), original)
        p.start.assert_not_called()

    def test_two_workers_share_six_shots_without_redeploying(self):
        spec = validate(SPEC)
        services = {"example": {"spec": spec}}
        keys = {"example": key_for(spec, "source")}
        members = {f"{shot:064x}": ["image"] for shot in range(6)}
        workers = [self.provisioner(Path("unused.json")) for _ in range(2)]
        workers[1].registry = workers[0].registry
        workers[0].registry[keys["example"]] = {
            "status": "ready", "version": "v1", "cache_fingerprint": "weights"}
        for worker in workers:
            worker.lock, worker.pool, worker.futures = threading.Lock(), Mock(), {}
            worker.deployed = Mock(return_value=True)
            worker.legacy_admissions = Mock(return_value={})
        with patch("orchestration.provisioning.remote_cache_fingerprint", return_value="weights") as check:
            for index, identity in enumerate(members):
                worker = workers[index % 2]
                self.assertEqual(worker.batch_keys(services, "batch"), keys)
                self.assertTrue(worker.admit_batch(services, keys, {"example": members}, identity))
        self.assertEqual(check.call_count, 2)
        for worker in workers:
            worker.deployed.assert_called_once()
            worker.pool.submit.assert_not_called()
            worker.command.assert_not_called()
