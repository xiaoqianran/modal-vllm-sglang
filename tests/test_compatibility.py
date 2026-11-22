"""Legacy Volume migration and Pika recovery fault injection, without network/GPU."""
import copy
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from runtime.tasks.task_identity import intent_digest
from runtime.utils.files import atomic_json, read_json
from runtime.config.model_cache import DOWNLOAD_MAX_WORKERS, component_specs, prepare_cache, verify_cache, checked_inventory
from runtime.serving.inference_execution import execute
from runtime.serving.model_runtime import create_app
from runtime.config.model_spec import discover, parameters

SPECS = discover()
MP4 = b'\0\0\0\x20ftyp' + b'x' * 32


class CompatibilityTests(unittest.TestCase):
    def test_default_hf_download_uses_eight_workers(self):
        spec = SPECS['moss.t2i']
        self.assertEqual(DOWNLOAD_MAX_WORKERS, 8)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hub = types.ModuleType('huggingface_hub')
            def snapshot(*args, **kwargs):
                self.assertEqual(kwargs['max_workers'], 8)
                path = root / 'hub' / 'snapshot'
                path.mkdir(parents=True, exist_ok=True)
                (path / 'model_index.json').write_text('{}')
                (path / 'weights.safetensors').write_bytes(b'weights')
                return str(path)
            hub.snapshot_download = snapshot
            with patch.dict(sys.modules, {'huggingface_hub': hub}):
                result = prepare_cache(spec, Mock(), root)
            self.assertTrue(result['ready'])
            self.assertTrue(verify_cache(spec, root)[0])

    def test_pika_overlay_keeps_materialization_with_eight_snapshot_workers(self):
        spec = SPECS['pika.t2va']
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            hub = types.ModuleType('huggingface_hub')
            hub.snapshot_download = Mock()
            fake = types.ModuleType('sglang.multimodal_gen.runtime.utils.hf_diffusers_utils')
            def snapshot(*args, **kwargs):
                calls.append(kwargs['max_workers'])
                folder = root / 'sglang' / 'materialized_models' / 'pika'
                folder.mkdir(parents=True, exist_ok=True)
                for name in ('model_index.json', '.sglang_overlay_materialized.json', 'weights.safetensors'):
                    (folder / name).write_bytes(b'weights')
                return str(folder)
            def materialize(model, **kwargs):
                self.assertEqual(model, spec['model'])
                self.assertTrue(kwargs['force_diffusers_model'])
                self.assertEqual(kwargs['revision'], spec['revision'])
                return fake.snapshot_download(model)
            fake.snapshot_download = snapshot
            fake.maybe_download_model = materialize
            # 模拟 SGLang 的同名模块，确保不会绕过 Overlay 物化入口。
            with patch.dict(sys.modules, {
                'huggingface_hub': hub,
                'sglang': types.ModuleType('sglang'),
                'sglang.multimodal_gen': types.ModuleType('sglang.multimodal_gen'),
                'sglang.multimodal_gen.runtime': types.ModuleType('sglang.multimodal_gen.runtime'),
                'sglang.multimodal_gen.runtime.utils': types.ModuleType('sglang.multimodal_gen.runtime.utils'),
                'sglang.multimodal_gen.runtime.utils.hf_diffusers_utils': fake,
            }):
                volume = Mock()
                result = prepare_cache(spec, volume, root)
                self.assertTrue(result['ready'])
                self.assertEqual(calls, [8])
                self.assertIs(fake.snapshot_download, snapshot)
                self.assertTrue(verify_cache(spec, root)[0])
                volume.commit.assert_called_once()

    def legacy(self, root, service):
        spec = SPECS[service + ('.t2va' if service == 'pika' else '.t2i')]
        old = {'manifest_version': 2 if service == 'pika' else 1}
        for name, config in component_specs(spec).items():
            relative = 'models--' + config['model'].replace('/', '--') + '/snapshots/' + (config['revision'] if config['revision'] != 'main' else 'a' * 40)
            path = root / ('sglang/materialized_models/pika' if service == 'pika' else 'hub/' + relative)
            files = {'moss': ['model_index.json', 'weights.safetensors'], 'pika': ['model_index.json', '.sglang_overlay_materialized.json', 'weights.safetensors'], 'minimax': ['model_index.json', 'pdd_config.json', 'pdd_fused_heads.safetensors', 'transformer/w.safetensors', 'vae/w.safetensors'] if name == 'model' else ['text_encoder/w.safetensors']}[service]
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

    def test_migrate_only_never_imports_downloaders_and_is_idempotent(self):
        for service in ('pika', 'minimax'):
            with self.subTest(service=service), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                spec, _ = self.legacy(root, service)
                volume = Mock()
                with patch.dict('sys.modules', {'huggingface_hub': None}):
                    first = prepare_cache(spec, volume, root, migrate_only=True)
                    second = prepare_cache(spec, volume, root, migrate_only=True)
                self.assertEqual(first['fingerprint'], second['fingerprint'])
                volume.commit.assert_called_once()

    def test_migrate_only_missing_or_damaged_cache_never_downloads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = SPECS['minimax.t2i']
            with patch.dict('sys.modules', {'huggingface_hub': None}):
                with self.assertRaisesRegex(RuntimeError, 'No existing'):
                    prepare_cache(spec, Mock(), root, migrate_only=True)
                spec, old = self.legacy(root, 'minimax')
                next(root.glob('hub/**/text_encoder/*.safetensors')).unlink()
                with self.assertRaises(RuntimeError):
                    prepare_cache(spec, Mock(), root, migrate_only=True)
            self.assertEqual(read_json(root / 'model-ready.json'), old)
            with self.assertRaises(ValueError):
                prepare_cache(spec, Mock(), root, migrate_only=True, refresh=True)

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
        for folder, name in [('models/001-moss-image', 'Moss'), ('models/002-pika-h3-flex', 'Pika'), ('models/005-minimax-h3-image', 'H3')]:
            app, model = create_app(Path(__file__).resolve().parents[1] / folder / 'model.json')
            self.assertIn(name, app.registered_classes)


class PikaRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        redirect = patch('runtime.serving.inference_execution.Path', wraps=Path)
        paths = redirect.start()
        paths.side_effect = lambda value: self.root if value == '/data' else Path(value)
        self.addCleanup(redirect.stop)
        factory = patch('runtime.serving.inference_api.SGLangVideoClient')
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
        with patch('runtime.utils.files.os.replace', side_effect=fail_media), self.assertRaises(OSError):
            self.run_batch()
        self.assertEqual(read_json(self.metadata)['job_id'], 'existing-job')
        self.assertEqual(list(stale.parent.glob('*.tmp')), [])
        self.assertEqual(self.run_batch()[0]['status'], 'completed')
        self.client.submit.assert_called_once()
        self.assertEqual(stale.read_bytes(), b'stale')
