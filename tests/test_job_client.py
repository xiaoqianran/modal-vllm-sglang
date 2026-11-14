"""Detached calls, durable receipts and recovery without GPU or network."""
import hashlib
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from shared.batch import intent_digest
from shared.files import atomic_json, read_json
from shared.job_client import run_jobs
from shared.model_cache import component_specs
from shared.model_service import ROOT
from shared.model_spec import validate


class JobClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.spec = validate(read_json(ROOT / "001-moss-image/model.json"))
        self.cache = {"version": 1, "cache_spec": intent_digest(component_specs(self.spec))}
        self.files = {}
        self.volume = Mock()
        def read(path):
            if path.endswith("/model-ready.json"):
                return [json.dumps(self.cache).encode()]
            if path not in self.files:
                raise FileNotFoundError(path)
            return [self.files[path]]
        self.volume.read_file.side_effect = read
        self.model = Mock()
        self.model.generate_one.spawn.side_effect = lambda *args: Mock(object_id=f"fc-{self.model.generate_one.spawn.call_count}")
        self.call = Mock()
        self.call.get.side_effect = TimeoutError()
        self.addCleanup(patch.stopall)
        patch("shared.modal_setup.model_volume", return_value=self.volume).start()
        patch("modal.Cls.from_name", return_value=Mock(return_value=self.model)).start()
        self.from_id = patch("modal.FunctionCall.from_id", return_value=self.call).start()
        patch("sys.stdout", new=io.StringIO()).start()
        self.task = self.root / "task.json"
        self.write_task()

    def write_task(self, count=1, prompt="boat"):
        atomic_json(self.task, {"version": 1, "name": "demo", "workflow": "moss-t2i",
                               "items": [{"id": f"{i:03}", "prompt": prompt} for i in range(count)]})

    def run_jobs(self, mode="enqueue", paths=None):
        return run_jobs(self.spec, [self.task] if paths is None else paths, root=self.root, mode=mode)

    def test_twenty_detached_calls_and_restart_do_not_resubmit(self):
        self.write_task(20)
        jobs = self.run_jobs()
        self.assertEqual(len(jobs), 20)
        self.assertEqual(self.model.generate_one.spawn.call_count, 20)
        self.assertTrue(all(job["call_id"] for job in jobs.values()))
        self.call.get.assert_not_called()  # enqueue never waits for new calls
        self.run_jobs()
        self.run_jobs("status", paths=[])
        self.assertEqual(self.model.generate_one.spawn.call_count, 20)
        self.call.get.assert_called_with(timeout=0)

    def test_lost_spawn_response_remains_uncertain(self):
        self.model.generate_one.spawn.side_effect = ConnectionError("lost response")
        with self.assertRaises(ConnectionError):
            self.run_jobs()
        jobs = self.run_jobs()
        self.assertEqual(next(iter(jobs.values()))["status"], "uncertain")
        self.model.generate_one.spawn.assert_called_once()

    def test_death_before_receipt_is_conservative(self):
        self.run_jobs()
        manifest = self.root / "outputs/.runner/jobs/moss.json"
        jobs = read_json(manifest)
        job = next(iter(jobs.values()))
        job.pop("call_id")
        job["status"] = "submitting"
        atomic_json(manifest, jobs)
        self.assertEqual(next(iter(self.run_jobs().values()))["status"], "uncertain")
        self.model.generate_one.spawn.assert_called_once()

    def test_changed_parameters_conflict_before_submission(self):
        self.run_jobs()
        self.write_task(prompt="different")
        with self.assertRaisesRegex(ValueError, "conflict"):
            self.run_jobs()
        self.model.generate_one.spawn.assert_called_once()

    def test_volume_completion_survives_expired_call_and_collects(self):
        jobs = self.run_jobs()
        job = next(iter(jobs.values()))
        data = b"\x89PNG\r\n\x1a\n" + b"x" * 32
        record = {"id": job["id"], "file": job["id"] + ".png", "status": "completed",
                  "intent_hash": job["intent_hash"], "execution_hash": job["execution_hash"],
                  "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        self.files[f"/{job['prefix']}/{job['id']}.json"] = json.dumps(record).encode()
        self.files[f"/{job['prefix']}/{record['file']}"] = data
        self.call.get.side_effect = RuntimeError("expired")
        self.run_jobs("collect", paths=[])
        self.assertEqual((self.root / "outputs/demo/001-main/000.png").read_bytes(), data)
        self.call.get.assert_not_called()
        self.run_jobs()
        self.model.generate_one.spawn.assert_called_once()

    def test_call_failure_never_authorizes_retry(self):
        self.run_jobs()
        self.call.get.side_effect = RuntimeError("container crashed")
        jobs = self.run_jobs()
        self.assertEqual(next(iter(jobs.values()))["status"], "uncertain")
        self.model.generate_one.spawn.assert_called_once()

    def test_conflicting_files_fail_before_any_spawn(self):
        second = self.root / "second.json"
        task = read_json(self.task)
        task["items"][0]["prompt"] = "changed"
        atomic_json(second, task)
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            self.run_jobs(paths=[self.task, second])
        self.model.generate_one.spawn.assert_not_called()

    def test_corrupt_download_does_not_publish_or_regenerate(self):
        job = next(iter(self.run_jobs().values()))
        record = {"id": job["id"], "file": job["id"] + ".png", "status": "completed",
                  "intent_hash": job["intent_hash"], "execution_hash": job["execution_hash"],
                  "sha256": "a" * 64}
        self.files[f"/{job['prefix']}/{job['id']}.json"] = json.dumps(record).encode()
        self.files[f"/{job['prefix']}/{record['file']}"] = b"damaged"
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.run_jobs("collect", paths=[])
        self.assertFalse((self.root / "outputs/demo/001-main/000.png").exists())
        self.run_jobs()
        self.model.generate_one.spawn.assert_called_once()

    def test_cache_change_conflicts_before_new_spawn(self):
        self.run_jobs()
        self.cache["resolved_revision"] = "changed"
        with self.assertRaisesRegex(ValueError, "conflict"):
            self.run_jobs()
        self.model.generate_one.spawn.assert_called_once()

    def remote_result(self, *, prompt="boat", status="completed"):
        self.write_task(prompt=prompt)
        from shared.model_service import load_task, prepare
        project, scene, operation, items = load_task(self.spec, self.task, root=self.root)
        signature = prepare(self.spec, items[0])[1]
        prefix = f"model-results/{self.spec['service']}/{project}/{scene}/{operation}-{intent_digest(self.spec)[:20]}"
        data = b"\x89PNG\r\n\x1a\n" + b"remote-image" * 4
        record = {"id": "000", "file": "000.png", "status": status,
                  "intent_hash": signature,
                  "execution_hash": intent_digest({"intent": signature, "cache": intent_digest(self.cache)}),
                  "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        self.files[f"/{prefix}/000.json"] = json.dumps(record).encode()
        self.files[f"/{prefix}/000.png"] = data
        return record, data

    def test_remote_collect_restores_on_fresh_computer_without_local_receipts(self):
        _, data = self.remote_result()
        self.assertFalse((self.root / "outputs/.runner/jobs/moss.json").exists())
        jobs = self.run_jobs("collect_remote")
        self.assertEqual(next(iter(jobs.values()))["status"], "completed")
        self.assertEqual((self.root / "outputs/demo/001-main/000.png").read_bytes(), data)
        self.assertFalse((self.root / "outputs/.runner/jobs/moss.json").exists())
        self.model.generate_one.spawn.assert_not_called()
        self.from_id.assert_not_called()
        self.run_jobs("collect_remote")
        self.assertEqual((self.root / "outputs/demo/001-main/000.png").read_bytes(), data)

    def test_remote_collect_missing_result_never_submits_or_creates_receipt(self):
        jobs = self.run_jobs("collect_remote")
        self.assertEqual(next(iter(jobs.values()))["status"], "not_found")
        self.assertFalse((self.root / "outputs/.runner/jobs/moss.json").exists())
        self.model.generate_one.spawn.assert_not_called()

    def test_remote_collect_noncompleted_result_does_not_download(self):
        self.remote_result(status="submitting")
        jobs = self.run_jobs("collect_remote")
        self.assertEqual(next(iter(jobs.values()))["status"], "submitting")
        self.assertFalse((self.root / "outputs/demo/001-main/000.png").exists())

    def test_remote_collect_rejects_changed_prompt(self):
        self.remote_result()
        self.write_task(prompt="changed prompt")
        with self.assertRaisesRegex(ValueError, "conflict"):
            self.run_jobs("collect_remote")
        self.assertFalse((self.root / "outputs/demo/001-main/000.png").exists())

    def test_remote_collect_rejects_cache_mismatch(self):
        self.remote_result()
        self.cache["resolved_revision"] = "different"
        with self.assertRaisesRegex(ValueError, "conflict"):
            self.run_jobs("collect_remote")
        self.assertFalse((self.root / "outputs/demo/001-main/000.png").exists())

    def test_remote_collect_rejects_bad_digest_and_existing_file(self):
        record, data = self.remote_result()
        record["sha256"] = "a" * 64
        from shared.model_service import load_task
        project, scene, operation, _ = load_task(self.spec, self.task, root=self.root)
        prefix = f"model-results/{self.spec['service']}/{project}/{scene}/{operation}-{intent_digest(self.spec)[:20]}"
        self.files[f"/{prefix}/000.json"] = json.dumps(record).encode()
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.run_jobs("collect_remote")
        target = self.root / "outputs/demo/001-main/000.png"
        self.assertFalse(target.exists())
        record["sha256"] = hashlib.sha256(data).hexdigest()
        self.files[f"/{prefix}/000.json"] = json.dumps(record).encode()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"different existing image")
        with self.assertRaises(FileExistsError):
            self.run_jobs("collect_remote")
        self.assertEqual(target.read_bytes(), b"different existing image")

    def test_remote_collect_rejects_unsafe_result_name(self):
        record, _ = self.remote_result()
        from shared.model_service import load_task
        project, scene, operation, _ = load_task(self.spec, self.task, root=self.root)
        prefix = f"model-results/{self.spec['service']}/{project}/{scene}/{operation}-{intent_digest(self.spec)[:20]}"
        record["file"] = "../outside.png"
        self.files[f"/{prefix}/000.json"] = json.dumps(record).encode()
        with self.assertRaisesRegex(ValueError, "Invalid completed result"):
            self.run_jobs("collect_remote")

    def test_remote_collect_uses_eight_workers_for_same_scene(self):
        from shared.model_service import load_task, prepare
        self.write_task(count=16)
        project, scene, operation, items = load_task(self.spec, self.task, root=self.root)
        prefix = f"model-results/{self.spec['service']}/{project}/{scene}/{operation}-{intent_digest(self.spec)[:20]}"
        data = b"\x89PNG\r\n\x1a\n" + b"parallel" * 12
        for item in items:
            record = {"id": item["id"], "file": item["id"] + ".png", "status": "completed",
                      "intent_hash": prepare(self.spec, item)[1],
                      "execution_hash": intent_digest({"intent": prepare(self.spec, item)[1],
                                                       "cache": intent_digest(self.cache)}),
                      "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
            self.files[f"/{prefix}/{item['id']}.json"] = json.dumps(record).encode()
            self.files[f"/{prefix}/{item['id']}.png"] = data
        previous = self.volume.read_file.side_effect
        active = 0
        maximum = 0
        lock = threading.Lock()
        def slow_read(path):
            nonlocal active, maximum
            if path.endswith(".png"):
                with lock:
                    active += 1
                    maximum = max(maximum, active)
                try:
                    time.sleep(0.08)
                    return previous(path)
                finally:
                    with lock:
                        active -= 1
            return previous(path)
        self.volume.read_file.side_effect = slow_read
        jobs = self.run_jobs("collect_remote")
        self.assertEqual(len(jobs), 16)
        self.assertEqual(maximum, 8)
        self.assertEqual(len(list((self.root / "outputs/demo/001-main").glob("*.png"))), 16)
        self.model.generate_one.spawn.assert_not_called()

    def test_parallel_collect_preserves_successful_receipts_on_other_failure(self):
        self.write_task(count=2)
        jobs = self.run_jobs()
        for index, job in enumerate(jobs.values()):
            data = b"\x89PNG\r\n\x1a\n" + b"x" * 32
            record = {"id": job["id"], "file": job["id"] + ".png", "status": "completed",
                      "intent_hash": job["intent_hash"], "execution_hash": job["execution_hash"],
                      "sha256": hashlib.sha256(data).hexdigest() if index == 0 else "b" * 64}
            self.files[f"/{job['prefix']}/{job['id']}.json"] = json.dumps(record).encode()
            self.files[f"/{job['prefix']}/{job['id']}.png"] = data
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.run_jobs("collect", paths=[])
        receipt = read_json(self.root / "outputs/.runner/jobs/moss.json")
        self.assertTrue(receipt["demo/001-main/000/image"]["collected"])
        self.assertNotIn("collected", receipt["demo/001-main/001/image"])
        self.assertTrue((self.root / "outputs/demo/001-main/000.png").exists())
        self.assertFalse((self.root / "outputs/demo/001-main/001.png").exists())


if __name__ == "__main__":
    unittest.main()
