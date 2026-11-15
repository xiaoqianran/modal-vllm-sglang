"""Snapshot lifecycle boundaries without allocating remote containers or GPUs."""
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from shared.files import atomic_json
from shared.model_service import create_app
from shared.model_spec import validate
from tests.test_model_service import SPEC


class MemorySnapshotTests(unittest.TestCase):
    def lifecycle(self, mode, *, changed=False):
        stack = ExitStack()
        self.addCleanup(stack.close)
        directory = stack.enter_context(tempfile.TemporaryDirectory())
        path = Path(directory) / 'model.json'
        atomic_json(path, {**SPEC, 'memory_snapshot': mode})
        cache = stack.enter_context(patch('shared.model_service.verify_cache',
                                         side_effect=[({}, 'original'), ({}, 'changed' if changed else 'original')]))
        stack.enter_context(patch('shared.model_service.command_argv', return_value=['engine']))
        popen = stack.enter_context(patch('shared.model_service.subprocess.Popen'))
        health = stack.enter_context(patch('shared.runtime.wait_for_health'))
        stop = stack.enter_context(patch('shared.runtime.stop_process'))
        monitor = stack.enter_context(patch('shared.runtime.monitor_processes'))
        resources = stack.enter_context(patch('shared.metrics.ResourceMonitor'))
        resources.return_value.snapshot.return_value = {}
        _, model = create_app(path)
        cls = model._get_user_cls()
        obj = cls()
        return cls, obj, cache, popen, health, stop, monitor, resources

    def test_capture_has_no_runtime_threads_or_gateway_and_restore_reuses_engine(self):
        cls, obj, cache, popen, health, stop, monitor, resources = self.lifecycle('gpu')
        cls.capture_engine._get_raw_f()(obj)
        self.assertEqual(popen.call_count, 1)
        resources.assert_not_called()
        monitor.assert_not_called()
        self.assertFalse(hasattr(obj, 'batch_lock'))
        cls.start._get_raw_f()(obj)
        self.assertEqual(popen.call_count, 2)  # existing engine plus fresh gateway
        self.assertEqual(cache.call_count, 2)
        self.assertEqual([call.kwargs['phase'] for call in cache.call_args_list],
                         ['engine_start', 'post_snapshot'])
        self.assertEqual(health.call_count, 3)
        resources.return_value.start.assert_called_once()
        monitor.assert_called_once()
        stop.assert_not_called()

    def test_disabled_snapshot_starts_engine_in_runtime_hook(self):
        cls, obj, cache, popen, health, stop, monitor, resources = self.lifecycle('off')
        cls.capture_engine._get_raw_f()(obj)
        popen.assert_not_called()
        cls.start._get_raw_f()(obj)
        self.assertEqual(popen.call_count, 2)
        self.assertEqual(cache.call_count, 1)
        self.assertGreaterEqual(obj.startup_seconds, obj.engine_initialization_seconds)

    def test_changed_cache_stops_captured_engine_before_gateway_starts(self):
        cls, obj, cache, popen, health, stop, monitor, resources = self.lifecycle('gpu', changed=True)
        cls.capture_engine._get_raw_f()(obj)
        with self.assertRaisesRegex(RuntimeError, 'redeploy'):
            cls.start._get_raw_f()(obj)
        self.assertEqual(popen.call_count, 1)
        stop.assert_called_once_with(obj.process)
        resources.return_value.start.assert_not_called()
        monitor.assert_not_called()

    def test_invalid_mode_is_rejected(self):
        for value in (True, None, {}, [], 'cpu', 'enabled'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate({**SPEC, 'memory_snapshot': value})


if __name__ == '__main__':
    unittest.main()
