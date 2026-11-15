import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from orchestration.provisioning import Provisioner, model_paths, source_version
from shared.config.model_spec import validate
from shared.utils.files import atomic_json
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
            atomic_json(root / "new/model.json", SPEC)
            self.assertEqual(list(model_paths(root)), ["example"])
            before = source_version(root)
            atomic_json(root / "new/model.json", {**SPEC, "revision": "b" * 40})
            self.assertNotEqual(before, source_version(root))
            atomic_json(root / "duplicate/model.json", SPEC)
            with self.assertRaises(ValueError):
                model_paths(root)

    def provisioner(self, path):
        p = Provisioner.__new__(Provisioner)
        p.backend = Mock()
        p.registry = Registry()
        p.version = "source"
        p.paths = {"example": path}
        p.command = Mock()
        p.deployed = Mock(side_effect=[False, True])
        return p

    def test_prepare_before_deploy_and_ready_after_verification(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "model.json"
            atomic_json(path, SPEC)
            p = self.provisioner(path)
            with patch("orchestration.provisioning.remote_cache_fingerprint", return_value="weights"), \
                 patch("orchestration.provisioning.source_version", return_value="source"):
                p.prepare(validate(SPEC), "key")
            self.assertEqual([c.args[0] for c in p.command.call_args_list], ["run", "deploy"])
            self.assertEqual(p.registry["key"]["status"], "ready")
            self.assertFalse(any(k.startswith("lock:") for k in p.registry))

    def test_failed_download_never_deploys_and_has_bounded_retries(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "model.json"
            atomic_json(path, SPEC)
            p = self.provisioner(path)
            p.command.side_effect = RuntimeError("download failed")
            with patch("orchestration.provisioning.time.sleep"):
                p.prepare(validate(SPEC), "key")
            self.assertEqual(p.command.call_count, 3)
            p.deployed.assert_not_called()
            self.assertEqual(p.registry["key"]["status"], "failed")

    def test_matching_deployment_is_reused_after_cache_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "model.json"
            atomic_json(path, SPEC)
            p = self.provisioner(path)
            p.deployed = Mock(return_value=True)
            with patch("orchestration.provisioning.remote_cache_fingerprint", return_value="weights"), \
                 patch("orchestration.provisioning.source_version", return_value="source"):
                p.prepare(validate(SPEC), "key")
            self.assertEqual([c.args[0] for c in p.command.call_args_list], ["run"])
            self.assertEqual(p.registry["key"]["status"], "ready")
