"""Local contract checks and best-effort telemetry; no GPU or network access."""
import io
import unittest
from unittest.mock import Mock, patch

from runtime.tasks.task_identity import intent_digest
from runtime.utils.files import read_json
from runtime.telemetry.sampler import ResourceMonitor, resource_sample
from runtime.serving.legacy_cache_preparer import run_download_cli
from runtime.tasks.task_spec import ROOT
from orchestration.sdk import check_deployment
from runtime.config.storage import VOLUME_NAME
from runtime.config.model_spec import validate


class ReadinessTests(unittest.TestCase):
    def test_prepare_modes_are_exclusive_before_remote_call(self):
        download, check = Mock(), Mock()
        for modes in ({"check_only": True, "refresh": True},
                      {"check_only": True, "migrate_only": True},
                      {"refresh": True, "migrate_only": True}):
            with self.assertRaises(ValueError):
                run_download_cli(download, check, **modes)
        download.remote.assert_not_called()
        check.remote.assert_not_called()
        with patch('sys.stdout', new=io.StringIO()):
            run_download_cli(download, check, migrate_only=True)
        download.remote.assert_called_once_with(refresh=False, migrate_only=True)

    def test_deployment_dry_run_never_contacts_modal(self):
        config = ROOT / 'models/002-pika-h3-flex/model.json'
        with patch('modal.Cls.from_name') as lookup, \
                patch('runtime.serving.modal_setup.model_volume') as volume, \
                patch('sys.stdout', new=io.StringIO()):
            check_deployment(validate(read_json(config)), dry_run=True)
        lookup.assert_not_called()
        volume.assert_not_called()

    def test_live_deployment_info_must_match_spec_cache_and_methods(self):
        for folder in ('models/002-pika-h3-flex', 'models/005-minimax-h3-image-sglang'):
            config = ROOT / folder / 'model.json'
            spec = validate(read_json(config))
            valid = {'spec_hash': intent_digest(spec), 'cache_fingerprint': 'prepared',
                     'methods': ['generate_one'], 'cloud_artifacts': 1, 'volume': VOLUME_NAME, 'telemetry_schema_version': 1}
            for update in ({}, {'spec_hash': 'stale'}, {'cache_fingerprint': 'stale'}, {'methods': []},
                           {'cloud_artifacts': 0}, {'volume': 'old-storage'}, {'telemetry_schema_version': 0}):
                with self.subTest(folder=folder, update=update), \
                        patch('orchestration.sdk.remote_cache_fingerprint', return_value='prepared'), \
                        patch('runtime.serving.modal_setup.model_volume'), \
                        patch('modal.Cls.from_name') as lookup, \
                        patch('sys.stdout', new=io.StringIO()):
                    model = lookup.return_value.return_value
                    model.deployment_info.remote.return_value = {**valid, **update}
                    if update:
                        with self.assertRaisesRegex(RuntimeError, 'contract mismatch'):
                            check_deployment(spec)
                    else:
                        self.assertEqual(check_deployment(spec)['cache_fingerprint'], 'prepared')
                    model.generate_one.spawn.assert_not_called()

    def test_input_concurrency_and_sampling_interval_validation(self):
        spec = read_json(ROOT / 'models/002-pika-h3-flex/model.json')
        for key in ('max_inputs', 'telemetry_interval'):
            for value in (0, -1, True, '8', 1.5):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    validate({**spec, key: value})
            self.assertEqual(validate({**spec, key: 8})[key], 8)


class MetricsTests(unittest.TestCase):
    def test_missing_metrics_never_fail_generation(self):
        with patch('runtime.telemetry.sampler.Path.read_text', side_effect=OSError()), \
                patch('runtime.telemetry.sampler.subprocess.run', side_effect=FileNotFoundError()):
            self.assertEqual(resource_sample(), {})

    def test_container_cpu_ram_and_gpu_samples(self):
        values = {'memory.current': '100', 'memory.peak': '200', 'cpu.stat': 'usage_usec 1500000\nnr_periods 10'}
        with patch('runtime.telemetry.sampler.Path.read_text', autospec=True, side_effect=lambda p: values[p.name]), \
                patch('runtime.telemetry.sampler.subprocess.run', return_value=Mock(stdout='1024, 80\n2048, 20\n')):
            sample = resource_sample()
        self.assertEqual(sample['ram_peak_bytes'], 200)
        self.assertEqual(sample['cpu_seconds'], 1.5)
        self.assertEqual(sample['gpus'][1]['used_mib'], 2048)

    def test_gpu_peak_is_preserved_when_current_usage_falls(self):
        monitor = ResourceMonitor()
        with patch('runtime.telemetry.sampler.resource_sample', side_effect=[
                {'gpus': [{'used_mib': 200}]}, {'gpus': [{'used_mib': 100}]}]):
            monitor.sample()
            result = monitor.snapshot()
        self.assertEqual(result['sampled_gpu_peak_mib'], [200])
        self.assertEqual(result['gpus'][0]['used_mib'], 100)
