"""File inventory integrity and stage timing, without remote resources."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.cache import file_inventory
from shared.model_cache import component_specs, verify_cache
from shared.batch import intent_digest
from shared.files import atomic_json
from tests.test_model_service import SPEC


class CacheValidationTests(unittest.TestCase):
    def test_full_validation_still_rejects_missing_extra_and_resized_files(self):
        for corruption in ('missing', 'extra', 'resized', 'empty'):
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                model = root / 'model'
                model.mkdir()
                weight = model / 'weight.safetensors'
                weight.write_bytes(b'weights')
                atomic_json(root / 'model-ready.json', {
                    'version': 1, 'cache_spec': intent_digest(component_specs(SPEC)),
                    'components': {'model': {'path': 'model', 'inventory': file_inventory(model)}}})
                verify_cache(SPEC, root)
                if corruption == 'missing':
                    weight.unlink()
                elif corruption == 'extra':
                    (model / 'unexpected.json').write_text('{}')
                else:
                    weight.write_bytes(b'' if corruption == 'empty' else b'changed size')
                with patch('sys.stdout', new_callable=io.StringIO) as output:
                    with self.assertRaises(RuntimeError):
                        verify_cache(SPEC, root, phase='post_snapshot')
                event = json.loads(output.getvalue())
                self.assertFalse(event['success'])
                self.assertEqual(event['validation'], 'full_inventory')

    def test_inventory_stats_each_regular_file_once_and_stays_sorted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('b.bin', 'a.bin'):
                (root / name).write_bytes(b'123')
            original = Path.stat
            calls = []

            def tracked(path, *args, **kwargs):
                if path.suffix == '.bin':
                    calls.append(path.name)
                return original(path, *args, **kwargs)

            with patch.object(Path, 'stat', tracked):
                inventory = file_inventory(root)
            self.assertEqual(list(inventory), ['a.bin', 'b.bin'])
            self.assertCountEqual(calls, ['a.bin', 'b.bin'])

    def test_success_timing_preserves_return_value(self):
        expected = ({'model': Path('/models/model')}, 'fingerprint')
        with patch('shared.model_cache._verify_cache_full', return_value=expected), \
                patch('shared.model_cache.time.perf_counter', side_effect=[10, 10.25]), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(verify_cache(SPEC, phase='engine_start'), expected)
        event = json.loads(output.getvalue())
        self.assertTrue(event['success'])
        self.assertEqual(event['seconds'], 0.25)
        self.assertEqual(event['phase'], 'engine_start')
