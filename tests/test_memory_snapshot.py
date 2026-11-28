"""Snapshot lifecycle boundaries without allocating remote containers or GPUs."""
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from runtime.utils.files import atomic_json
from runtime.serving.model_runtime import create_app
from runtime.config.model_spec import validate
from tests.test_model_service import SPEC


class MemorySnapshotTests(unittest.TestCase):
    def lifecycle(self, mode, *, changed=False):
        stack = ExitStack()
        self.addCleanup(stack.close)
        directory = stack.enter_context(tempfile.TemporaryDirectory())
        path = Path(directory) / 'model.json'
        atomic_json(path, {**SPEC, 'memory_snapshot': mode})
        cache = stack.enter_context(patch('runtime.serving.model_runtime.verify_cache',
                                         side_effect=[({}, 'original'), ({}, 'changed' if changed else 'original')]))
        stack.enter_context(patch('runtime.serving.model_runtime.command_argv', return_value=['engine']))
        popen = stack.enter_context(patch('runtime.serving.model_runtime.subprocess.Popen'))
        popen.return_value.poll.return_value = None
        stack.enter_context(patch('runtime.serving.inference_gateway.make_server'))
        stack.enter_context(patch('runtime.serving.inference_gateway.serve_gateway'))
        health = stack.enter_context(patch('runtime.serving.process_lifecycle.wait_for_health'))
        stop = stack.enter_context(patch('runtime.serving.process_lifecycle.stop_process'))
        monitor = stack.enter_context(patch('runtime.serving.process_lifecycle.monitor_processes'))
        resources = stack.enter_context(patch('runtime.telemetry.sampler.ResourceMonitor'))
        resources.return_value.snapshot.return_value = {}
        _, model = create_app(path)
        cls = model._get_user_cls()
        obj = cls()
        stack.enter_context(patch.object(cls, '_load_spec', return_value=validate({**SPEC, 'memory_snapshot': mode})))

        def close_runtime():
            # Run before restoring mocks; failed starts and explicit stops already clean up.
            stopping = getattr(obj, 'stopping', None)
            if stopping is not None and not stopping.is_set():
                cls.stop._get_raw_f()(obj)
            writer = getattr(obj, 'result_writer', None)
            if writer is not None:
                self.assertFalse(writer.thread.is_alive(), 'Result writer must stop with its runtime')

        stack.callback(close_runtime)
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

    def test_disabled_snapshot_starts_engine_only_on_demand_once(self):
        cls, obj, cache, popen, health, stop, monitor, resources = self.lifecycle('off')
        self.assertFalse(hasattr(cls, 'capture_engine'))
        cls.start._get_raw_f()(obj)
        popen.assert_not_called()
        monitor.assert_not_called()
        health.assert_not_called()
        self.assertEqual(obj.engine_initialization_seconds, 0)
        self.assertEqual(cache.call_count, 1)
        self.assertEqual(cache.call_args.kwargs['phase'], 'runtime_setup')
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: obj._ensure_engine(), range(12)))
        popen.assert_called_once()
        monitor.assert_called_once()
        health.assert_called_once()
        self.assertEqual(cache.call_count, 1)

    def test_lazy_engine_failure_remains_retryable_before_submit(self):
        cls, obj, cache, popen, health, stop, monitor, resources = self.lifecycle('off')
        cls.start._get_raw_f()(obj)
        health.side_effect = [RuntimeError("startup failed"), None]
        with self.assertRaisesRegex(RuntimeError, "startup failed"):
            obj._ensure_engine()
        self.assertIsNone(obj.monitor)
        stop.assert_called_once()
        obj._ensure_engine()
        self.assertEqual(popen.call_count, 2)
        monitor.assert_called_once()
        self.assertEqual(cache.call_count, 1)

    def test_recovery_only_container_can_stop_without_engine(self):
        cls, obj, cache, popen, health, stop, monitor, resources = self.lifecycle('off')
        cls.start._get_raw_f()(obj)
        writer = obj.result_writer
        self.assertTrue(writer.thread.is_alive())
        self.assertFalse(writer.thread.daemon)
        cls.stop._get_raw_f()(obj)
        self.assertFalse(writer.thread.is_alive())
        popen.assert_not_called()
        stop.assert_not_called()
        obj.gateway_server.server_close.assert_called_once()

    def test_changed_cache_stops_captured_engine_before_gateway_starts(self):
        cls, obj, cache, popen, health, stop, monitor, resources = self.lifecycle('gpu', changed=True)
        cls.capture_engine._get_raw_f()(obj)
        with self.assertRaisesRegex(RuntimeError, 'redeploy'):
            cls.start._get_raw_f()(obj)
        self.assertEqual(popen.call_count, 1)
        stop.assert_called_once_with(obj.process)
        resources.return_value.start.assert_called_once()
        resources.return_value.stop.assert_called_once()
        monitor.assert_not_called()

    def test_invalid_mode_is_rejected(self):
        for value in (True, None, {}, [], 'cpu', 'enabled'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate({**SPEC, 'memory_snapshot': value})




class SnapshotEvidenceTests(unittest.TestCase):
    def test_capture_and_restore_are_distinguished_by_container_identity(self):
        from runtime.serving.model_runtime import snapshot_evidence
        origin = {"container_id": "ta-a", "gpu_name": "RTX PRO 6000",
                  "driver_version": "590", "cpu_flags_hash": "cpu-a"}
        same = snapshot_evidence(origin, dict(origin), "capture")
        self.assertEqual(same["relation"], "capture_origin")
        self.assertTrue(same["gpu_matches"])
        self.assertTrue(same["driver_matches"])
        self.assertTrue(same["cpu_flags_match"])
        other = snapshot_evidence(origin, {**origin, "container_id": "ta-b"}, "capture")
        self.assertEqual(other["relation"], "different_container")
        self.assertTrue(other["restored_from_capture"])
        self.assertEqual(other["capture_id"], "capture")
        mismatch = snapshot_evidence(origin, {**origin, "container_id": "ta-b",
                                               "driver_version": "600",
                                               "cpu_flags_hash": "cpu-b"}, "capture")
        self.assertFalse(mismatch["cpu_flags_match"])
        self.assertTrue(mismatch["gpu_matches"])
        self.assertFalse(mismatch["driver_matches"])
        unknown = snapshot_evidence({}, {}, None)
        self.assertEqual(unknown["relation"], "unknown")
        self.assertFalse(unknown["restored_from_capture"])
        self.assertIsNone(unknown["gpu_matches"])

    def test_environment_probe_failure_is_nonfatal(self):
        from runtime.serving.model_runtime import snapshot_environment
        with patch.dict("os.environ", {"MODAL_TASK_ID": "ta-current"}), \
             patch("runtime.serving.model_runtime.subprocess.run", side_effect=OSError("unavailable")):
            evidence = snapshot_environment(SPEC)
        self.assertEqual(evidence["container_id"], "ta-current")
        self.assertIsNone(evidence["gpu_name"])
        self.assertIsNone(evidence["driver_version"])


if __name__ == '__main__':
    unittest.main()
