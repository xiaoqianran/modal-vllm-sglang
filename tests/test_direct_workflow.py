"""Independent jobs bypass CPU orchestration without weakening admission safety."""
from pathlib import Path
import json
import runpy
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from orchestration import sdk, state
from orchestration.provisioning import Provisioner
from pipeline import s01_build_plan as runner
from pipeline import s02_submit_shots as client
from runtime.config.storage import volume_key
from runtime.telemetry.recorder import close_local
from tests.test_provisioning import Registry
from tests.test_sdk_orchestration import FakeBackend


class DirectWorkflowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.addCleanup(close_local, self.root / ".runtime/logs")
        shutil.copytree(runner.ROOT / "workflows", self.root / "workflows")
        task = self.root / "tasks/006-direct.json"
        state.save(task, {"version": 1, "name": "006-direct", "workflow": "moss-t2i",
                          "items": [{"id": f"{i:03}", "prompt": "boat"} for i in range(1, 13)]})
        self.directory = runner.new_run(runner.build_plan(task, root=self.root), root=self.root)
        self.backend = FakeBackend()
        self.backend.collect_outputs = False
        self.backend.submit_workflow = Mock(side_effect=AssertionError("CPU workflow not required"))
        self.backend.fingerprint = Mock(side_effect=AssertionError("Use the verified preparation fingerprint"))
        self.backend.poll = Mock(wraps=self.backend.poll)
        self.backend.submit = Mock(wraps=self.backend.submit)
        self.provisioner = Provisioner.__new__(Provisioner)
        self.provisioner.backend = self.backend
        self.provisioner.registry = Registry({"model-key": {"status": "ready", "cache_fingerprint": "verified"}})
        self.provisioner.batch_keys = Mock(return_value={"moss": "model-key"})
        self.provisioner.start = Mock(side_effect=AssertionError("Orchestrator must not be provisioned"))
        self.provisioner.legacy_admissions = Mock(return_value={})
        self.backend.provisioner = self.provisioner

    def tick(self, item):
        return client.tick_shot(self.directory, item, self.backend, {}, None)

    def test_twelve_jobs_queue_directly_and_release_admissions(self):
        for i in range(1, 13):
            self.tick(f"{i:03}")
        _, current = sdk.snapshot(self.directory)
        self.assertEqual(len(current["direct_runs"]), 12)
        self.assertNotIn("cloud_runs", current)
        self.assertEqual(self.backend.submit.call_count, 12)
        self.backend.poll.assert_not_called()
        self.backend.submit_workflow.assert_not_called()
        self.provisioner.start.assert_not_called()
        self.assertTrue(all(c.args[3] == "verified" for c in self.backend.submit.call_args_list))
        self.assertTrue(all(r["status"] == "queued" for r in current["jobs"].values()))
        self.assertEqual(len(self.provisioner.registry["admitted:model-key"]), 12)
        for i in range(1, 13):
            self.tick(f"{i:03}")
        self.assertTrue(self.provisioner.drained("model-key"))
        _, current = sdk.snapshot(self.directory)
        self.assertTrue(all(r["terminal_published"] for r in current["direct_runs"].values()))
        self.assertTrue(all(r["status"] == "completed" for r in current["jobs"].values()))
        for i in range(1, 13):
            self.tick(f"{i:03}")
        self.assertEqual(self.backend.submit.call_count, 12)

    def test_failed_preparation_does_not_submit_or_provision_cpu(self):
        self.provisioner.registry["model-key"] = {"status": "failed", "error": "version mismatch"}
        self.tick("001")
        _, current = sdk.snapshot(self.directory)
        self.assertEqual(current["jobs"]["image/001"]["status"], "failed")
        self.backend.submit.assert_not_called()
        self.provisioner.start.assert_not_called()

    def test_remote_result_proves_drain_after_local_worker_disappears(self):
        self.tick("001")
        _, current = sdk.snapshot(self.directory)
        receipt = current["direct_runs"]["001"]
        key = receipt["preparation"]
        proof = self.provisioner.registry[self.provisioner.direct_proof_key(key, receipt["id"])]
        self.backend.volume.read_file.side_effect = FileNotFoundError
        self.assertFalse(self.provisioner.direct_drained(key, receipt["id"], ["image/001"]))
        result = {"status": "completed", "id": proof["id"], "file": proof["id"] + ".png",
                  "bytes": 64, "sha256": "a" * 64, "intent_hash": proof["intent_hash"],
                  "execution_hash": proof["execution_hash"]}
        self.backend.volume.read_file.side_effect = None
        self.backend.volume.read_file.return_value = [json.dumps(result).encode()]
        self.assertTrue(self.provisioner.direct_drained(key, receipt["id"], ["image/001"]))
        # The remaining eleven unsubmitted reservations still protect the rollout.
        self.assertFalse(self.provisioner.drained(key))
        self.assertNotIn(receipt["id"], self.provisioner.registry["admitted:" + key])

    def test_ambiguous_spawn_is_never_replayed_and_preflight_cannot_cancel_it(self):
        submit = self.backend.submit.side_effect
        def lost(*args):
            FakeBackend.submit(self.backend, *args)
            raise OSError("reply lost")
        self.backend.submit.side_effect = lost
        with self.assertRaises(OSError):
            self.tick("001")
        _, current = sdk.snapshot(self.directory)
        self.assertEqual(current["jobs"]["image/001"]["status"], "uncertain")
        receipt = current["direct_runs"]["001"]
        self.assertFalse(self.provisioner.record_direct(receipt["preparation"], receipt["id"],
                                                        "image/001", {"status": "failed"}))
        self.backend.submit.side_effect = submit
        self.tick("001")
        self.assertEqual(self.backend.submit.call_count, 1)

    def test_old_cloud_receipts_keep_the_cloud_route(self):
        plan, current = sdk.snapshot(self.directory)
        current["cloud_runs"] = {"001": {"id": "existing"}}
        self.assertFalse(client.direct_scene(plan, current))

    def test_managed_deploy_refuses_manual_or_missing_version(self):
        entry = runner.ROOT / "orchestration/provision_entry.py"
        config = runner.ROOT / "models/001-moss-image/model.json"
        for version in ("", "manual"):
            with self.subTest(version=version), patch.dict("os.environ",
                    {"MODEL_CONFIG": str(config), "MODEL_PREPARE": "0", "MODEL_DEPLOY_VERSION": version}):
                with self.assertRaisesRegex(ValueError, "source/cache version hash"):
                    runpy.run_path(str(entry))


if __name__ == "__main__":
    unittest.main()
