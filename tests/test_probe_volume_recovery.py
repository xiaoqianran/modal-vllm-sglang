import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace

from orchestration.provisioning import Provisioner
from runtime.serving.deployment_probe import probe_name, register_probes, PROBE_OPTIONS
from runtime.utils.files import download_volume_file, read_json
from runtime.utils.volume_download import _recover_blocks


class DeploymentProbeTests(unittest.TestCase):
    version = "a" * 64

    def setUp(self):
        self.p = Provisioner.__new__(Provisioner)
        self.p.backend = Mock()
        self.modal = self.p.backend.modal
        self.modal.exception.NotFoundError = LookupError
        self.probe = self.modal.Function.from_name.return_value
        self.call = self.probe.spawn.return_value
        self.call.get.return_value = self.version
        self.tag = probe_name(self.version)
        self.info = self.modal.App.lookup.return_value.info

    def app(self, revision, *tags):
        return SimpleNamespace(lifecycle=SimpleNamespace(version=revision),
                               functions={tag: "fu-test" for tag in tags})

    def test_managed_probe_never_warms_legacy_container(self):
        self.info.return_value = self.app(2, "provision_info")
        self.assertFalse(self.p.deployed("model", self.version))
        self.modal.Function.from_name.assert_not_called()

    def test_versioned_probe_uses_current_tag_without_paid_revision_retention(self):
        self.info.return_value = self.app(3, self.tag)
        self.assertTrue(self.p.deployed("model", self.version))
        self.modal.Function.from_name.assert_called_once_with("model", self.tag)
        self.call.get.assert_called_once_with(timeout=35)
        self.assertEqual(self.info.call_count, 2)
        self.assertEqual(self.p.probe_revision('model', self.version), 3)

    def test_rollout_race_cannot_certify_previous_revision(self):
        self.info.side_effect = [self.app(3, self.tag), self.app(4, "provision_info")]
        self.assertFalse(self.p.deployed("model", self.version))

    def test_probe_timeout_cancels_only_probe_call(self):
        self.info.return_value = self.app(3, self.tag)
        self.call.get.side_effect = TimeoutError
        self.assertFalse(self.p.deployed("model", self.version))
        self.call.cancel.assert_called_once_with()

    def test_gpu_handle_is_not_reused_across_source_versions_and_guards_execution(self):
        from orchestration.sdk import Backend
        from tests.test_model_service import SPEC
        with patch("runtime.serving.modal_setup.model_volume", return_value=Mock()):
            backend = Backend()
        backend.modal = Mock()
        backend.submission_ledger = Mock()
        backend.submission_ledger.return_value.submit.side_effect = (
            lambda prefix, item, digest, spawn: spawn())
        spec = {**SPEC, "app_name": "test-app", "modal_class": "Model"}
        backend.submit(spec, {"id": "one", "prompt": "lake", "seed": 1},
                       "prefix", "cache", deployment_version="a" * 64)
        backend.submit(spec, {"id": "two", "prompt": "lake", "seed": 2},
                       "prefix", "cache", deployment_version="a" * 64)
        backend.submit(spec, {"id": "three", "prompt": "lake", "seed": 3},
                       "prefix", "cache", deployment_version="b" * 64)
        self.assertEqual([c.kwargs for c in backend.modal.Cls.from_name.call_args_list], [{}, {}])
        spawn = backend.modal.Cls.from_name.return_value.return_value.generate_one.spawn
        self.assertEqual([c.kwargs["expected_deployment_version"] for c in spawn.call_args_list],
                         ["a" * 64, "a" * 64, "b" * 64])
        self.assertEqual(len(backend.models), 2)

    def test_source_identity_is_invariant_under_checkout_line_endings(self):
        from orchestration.deployment_state import digest_files
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "model.py"
            spec = root / "model.json"
            source.write_bytes(b"def version():\n    return 1\n")
            spec.write_bytes(b'{\n  "service": "test"\n}\n')
            original = digest_files(root, [source, spec])
            for path in (source, spec):
                path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
            self.assertEqual(digest_files(root, [spec, source]), original)
            source.write_bytes(b"def version():\r\n    return 2\r\n")
            self.assertNotEqual(digest_files(root, [source, spec]), original)

    def test_stale_gpu_version_rejected_before_accessing_engine(self):
        from runtime.serving.deployment_probe import assert_deployment_version
        from runtime.serving.model_runtime import ModelRuntime
        with patch.dict("os.environ", {"MODEL_DEPLOY_VERSION": "a" * 64}):
            assert_deployment_version("a" * 64)
            assert_deployment_version(None)
            with self.assertRaisesRegex(RuntimeError, "Deployment source version mismatch"):
                # No model state or GPU has been initialized.
                ModelRuntime.generate_one._get_raw_f()(ModelRuntime(), {}, "prefix", "cache", "spec",
                                          expected_deployment_version="b" * 64)

    def test_workflow_version_uses_unique_tag_without_retention(self):
        from pipeline.s02_submit_shots import Backend
        from orchestration.protocol import APP_NAME
        with patch("runtime.serving.modal_setup.model_volume", return_value=Mock()):
            backend = Backend()
        backend.modal = Mock()
        backend.submit_workflow({}, "id", deployment_version="a" * 64)
        backend.modal.Function.from_name.assert_called_once_with(APP_NAME, "run_shot_" + "a" * 32)
        backend.modal.Function.from_name.return_value.spawn.assert_called_once_with(
            {}, "id", expected_deployment_version="a" * 64)

    def test_probe_contract(self):
        app = Mock()
        register_probes(app, "cpu-image", self.version)
        self.assertEqual([c.kwargs["name"] for c in app.function.call_args_list],
                         ["provision_info", self.tag])
        for c in app.function.call_args_list:
            self.assertEqual(c.kwargs["env"], {"MODEL_DEPLOY_VERSION": self.version})
            self.assertTrue(c.kwargs["single_use_containers"])
            self.assertEqual(c.kwargs["scaledown_window"], 2)
            self.assertEqual(c.kwargs["retries"], 0)
        self.assertTrue(PROBE_OPTIONS["single_use_containers"])


class ResumableVolumeTests(unittest.TestCase):
    chunks = [b"abc", b"defg", b"hi"]
    blob = b"".join(chunks)
    identity = {"volume": "vo-test", "path": "/file",
                "sha256": hashlib.sha256(blob).hexdigest()}

    def run_transfer(self, target, fetch, **kwargs):
        return asyncio.run(_recover_blocks(
            target, self.identity, len(self.blob),
            [len(c) for c in self.chunks], fetch, **kwargs))

    def test_fresh_download_hashes_in_memory_and_publishes(self):
        async def fetch(i, pos, length):
            return self.chunks[i]
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "image"
            with patch("runtime.utils.files.sha256_file", side_effect=AssertionError("full re-read")):
                result = self.run_transfer(target, fetch)
            self.assertEqual(target.read_bytes(), self.blob)
            self.assertEqual(result["downloaded_bytes"], len(self.blob))
            self.assertEqual(result["reused_bytes"], 0)
            self.assertFalse(target.with_name(target.name + ".volume-part.json").exists())

    def test_failed_block_keeps_other_blocks_and_only_retries_missing(self):
        seen = []
        async def fetch(i, pos, length):
            seen.append(i)
            if i == 1:
                raise OSError("network interrupted")
            return self.chunks[i]
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "image"
            with self.assertRaises(OSError):
                self.run_transfer(target, fetch)
            self.assertFalse(target.exists())
            cp = target.with_name(target.name + ".volume-part.json")
            self.assertEqual(set(read_json(cp)["blocks"]), {"0", "2"})
            seen.clear()
            async def recover(i, pos, length):
                seen.append(i)
                return self.chunks[i]
            stats = self.run_transfer(target, recover)
            self.assertEqual(seen, [1])
            self.assertEqual(stats["reused_bytes"], 5)
            self.assertEqual(stats["downloaded_bytes"], 4)
            self.assertEqual(target.read_bytes(), self.blob)

    def test_corrupt_local_block_is_refetched_and_valid_sibling_reused(self):
        async def fetch(i, pos, length):
            if i == 1:
                raise OSError("network interrupted")
            return self.chunks[i]
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "image"
            with self.assertRaises(OSError):
                self.run_transfer(target, fetch)
            with target.with_name(target.name + ".volume-part").open("r+b") as f:
                f.write(b"BAD")
            seen = []
            async def recover(i, pos, length):
                seen.append(i)
                return self.chunks[i]
            result = self.run_transfer(target, recover)
            self.assertEqual(seen, [0, 1])
            self.assertEqual(result["reused_bytes"], 2)
            self.assertEqual(target.read_bytes(), self.blob)

    def test_wrong_digest_does_not_replace_existing_output(self):
        async def fetch(i, pos, length):
            return b"x" * length
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "image"
            target.write_bytes(b"old")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                self.run_transfer(target, fetch)
            self.assertEqual(target.read_bytes(), b"old")
            self.assertFalse(target.with_name(target.name + ".volume-part").exists())

    def test_small_download_does_not_reread_for_sha256(self):
        volume = Mock()
        volume.read_file.return_value = iter(self.chunks)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "image"
            with patch("runtime.utils.files.sha256_file", side_effect=AssertionError("full re-read")):
                download_volume_file(volume, "/image", target,
                                     expected_sha256=self.identity["sha256"])
            self.assertEqual(target.read_bytes(), self.blob)


if __name__ == "__main__":
    unittest.main()
