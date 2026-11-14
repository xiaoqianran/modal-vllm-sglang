"""Legacy Volume migration and Pika recovery fault injection, without network/GPU."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from shared.batch import intent_digest
from shared.files import atomic_json, read_json
from shared.model_cache import component_specs, prepare_cache, verify_cache, checked_inventory
from shared.model_service import execute, create_app
from shared.model_spec import discover, parameters

SPECS = discover()
MP4 = b'\0\0\0\x20ftyp' + b'x' * 32


class CompatibilityTests(unittest.TestCase):
    def legacy(self, root, service):
        spec = SPECS[service + ('.t2va' if service == 'pika' else '.t2i')]
        old = {'manifest_version': 2 if service == 'pika' else 1}
        for name, config in component_specs(spec).items():
            relative = 'models--' + config['model'].replace('/', '--') + '/snapshots/' + (config['revision'] if config['revision'] != 'main' else 'a' * 40)
            path = root / ('sglang/materialized_models/pika' if service == 'pika' else 'hub/' + relative)
            files = {'moss': ['config.json', 'weights.safetensors'], 'pika': ['model_index.json', '.sglang_overlay_materialized.json', 'weights.safetensors'], 'minimax': ['model_index.json', 'pdd_config.json', 'pdd_fused_heads.safetensors', 'transformer/w.safetensors', 'vae/w.safetensors'] if name == 'model' else ['text_encoder/w.safetensors']}[service]
            for file in files:
                target = path / file
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b'weights')
            inventory = checked_inventory(path, config)
            entry = {'repo_id': config['model'], 'revision': config['revision'], 'snapshot_relative': relative, 'inventory': inventory, 'file_count': len(inventory), 'total_bytes': sum(inventory.values())}
            if service == 'pika':
                entry['materialized_path'] = str(path)
            if service == 'minimax':
                old['image' if name == 'model' else name] = entry
            else:
                old.update(entry)
        atomic_json(root / 'model-ready.json', old)
        return spec, old

    def test_all_legacy_caches_migrate_without_download_or_weight_copy(self):
        for service in ('moss', 'pika', 'minimax'):
            with self.subTest(service=service), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                spec, old = self.legacy(root, service)
                before = {p: p.stat().st_mtime_ns for p in root.rglob('*') if p.is_file() and p.name != 'model-ready.json'}
                volume = Mock()
                with patch.dict('sys.modules', {'huggingface_hub': None}):
                    result = prepare_cache(spec, volume, root)
                    again = prepare_cache(spec, volume, root)
                self.assertEqual(result['fingerprint'], again['fingerprint'])
                self.assertEqual(read_json(root / 'model-ready.legacy.json'), old)
                self.assertTrue(verify_cache(spec, root)[0])
                self.assertEqual(before, {p: p.stat().st_mtime_ns for p in before})
                volume.commit.assert_called_once()

    def test_corrupt_legacy_inventory_never_publishes_new_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec, old = self.legacy(root, 'minimax')
            next(root.glob('hub/**/text_encoder/*.safetensors')).write_bytes(b'damaged-weights')
            with self.assertRaisesRegex(RuntimeError, 'incomplete'):
                prepare_cache(spec, Mock(), root)
            self.assertEqual(read_json(root / 'model-ready.json'), old)
            self.assertFalse((root / 'model-ready.legacy.json').exists())

    def test_legacy_wrong_identity_and_path_are_rejected(self):
        for update in ({'repo_id': 'wrong/model'}, {'snapshot_relative': '../../outside'}):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                spec, old = self.legacy(root, 'moss')
                atomic_json(root / 'model-ready.json', {**old, **update})
                with self.assertRaises(RuntimeError):
                    prepare_cache(spec, Mock(), root)

    def test_duration_follows_seconds_and_explicit_mismatch_is_rejected(self):
        spec = SPECS['pika.t2va']
        original = copy.deepcopy(spec)
        self.assertEqual(parameters(spec, {'seconds': 6})['target']['duration_seconds'], 6)
        self.assertEqual(spec, original)
        with self.assertRaises(ValueError):
            parameters(spec, {'seconds': 6, 'target': spec['defaults']['target']})

    def test_existing_deployment_class_names_are_registered(self):
        for folder, name in [('001-moss-image', 'Moss'), ('002-pika-h3-flex', 'Pika'), ('005-minimax-h3-image', 'H3')]:
            app, model = create_app(Path(__file__).resolve().parents[1] / folder / 'model.json')
            self.assertIn(name, app.registered_classes)


class PikaRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        redirect = patch('shared.model_service.Path', wraps=Path)
        paths = redirect.start()
        paths.side_effect = lambda value: self.root if value == '/mnt/modal' else Path(value)
        self.addCleanup(redirect.stop)
        factory = patch('shared.inference_api.SGLangVideoClient')
        self.client = factory.start().return_value
        self.addCleanup(factory.stop)
        self.client.submit.return_value = 'existing-job'
        self.client.content.return_value = MP4
        self.volume = Mock()
        self.spec = SPECS['pika.t2va']
        self.item = {'id': '001', 'prompt': 'boat', 'seed': 42}
        self.metadata = self.root / 'batch/001.json'

    def run_batch(self):
        return list(execute(self.spec, [self.item], 'batch', self.volume))

    def test_commits_precede_submit_and_poll(self):
        observations = []
        self.client.submit.side_effect = lambda *a, **kw: (observations.append((self.volume.commit.call_count, read_json(self.metadata)['status'])) or 'existing-job')
        self.client.wait.side_effect = lambda *a, **kw: observations.append((self.volume.commit.call_count, read_json(self.metadata)['job_id']))
        self.run_batch()
        self.assertEqual(observations, [(1, 'submitting'), (2, 'existing-job')])

    def test_timeout_never_resubmits_unknown_job(self):
        self.client.submit.side_effect = TimeoutError('lost response')
        with self.assertRaises(TimeoutError):
            self.run_batch()
        with self.assertRaisesRegex(RuntimeError, 'Uncertain'):
            self.run_batch()
        self.client.submit.assert_called_once()

    def test_commit_failure_before_submit_stops_generation(self):
        self.volume.commit.side_effect = OSError('commit failed')
        with self.assertRaises(OSError):
            self.run_batch()
        self.client.submit.assert_not_called()

    def test_publish_failure_preserves_job_and_ignores_stale_part(self):
        stale = self.root / 'batch/001.mp4.part'
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b'stale')
        import os
        replace = os.replace
        def fail_media(source, target):
            if str(target).endswith('.mp4'):
                raise OSError('publish failed')
            return replace(source, target)
        with patch('shared.files.os.replace', side_effect=fail_media), self.assertRaises(OSError):
            self.run_batch()
        self.assertEqual(read_json(self.metadata)['job_id'], 'existing-job')
        self.assertEqual(list(stale.parent.glob('*.tmp')), [])
        self.assertEqual(self.run_batch()[0]['status'], 'completed')
        self.client.submit.assert_called_once()
        self.assertEqual(stale.read_bytes(), b'stale')
