"""Immutable evidence, failure isolation and a complete simulated model handoff."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import io
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from orchestration import runner, sdk
from shared.telemetry import schema
from shared.telemetry.collector import collect, collect_cloud, collect_local, ingest
from shared.telemetry.recorder import Recorder, close_local, flush_local
from shared.telemetry.report import task_report
from shared.telemetry.storage import Writer
from shared.serving.model_service import execute
from shared.utils.files import atomic_json, read_json
from tests.test_sdk_orchestration import PNG, MP4


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "outputs"
        self.addCleanup(close_local, self.output)

    def writer(self, cloud=False):
        writer = Writer(self.root / "cloud/telemetry" if cloud else self.output / ".telemetry/raw/local",
                        relative="telemetry" if cloud else "")
        self.addCleanup(writer.close)
        return writer

    def test_span_failure_records_error_type_without_exception_text(self):
        writer = self.writer()
        recorder = Recorder(writer, task_id="006-demo/001-main", job_key="image/001")
        with self.assertRaisesRegex(OSError, "private-token"):
            with recorder.span("generate"):
                raise OSError("private-token")
        writer.flush()
        result = task_report(self.output, "006-demo/001-main")
        span = result["stages"][0]
        self.assertEqual(span["status"], "failed")
        self.assertEqual(span["attributes"]["error_type"], "OSError")
        self.assertNotIn("private-token", json.dumps(result))
        self.assertGreaterEqual(span["duration_ms"], 0)
        self.assertIsNone(result["platform_wait_ms"])
        self.assertIsNone(result["snapshot_restore_ms"])

    def test_repeated_collection_deduplicates_and_detects_identity_conflicts(self):
        writer = self.writer()
        with Recorder(writer, task_id="006-demo/001-main").span("register"):
            pass
        writer.flush()
        first = collect_local(self.output)
        self.assertEqual(first["events"], 2)
        self.assertEqual(collect_local(self.output)["events"], 0)
        source = next(writer.directory.glob("*.jsonl"))
        records = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
        records[0]["task_id"] = "006-other/001-main"
        conflict = writer.directory / "conflict.jsonl"
        conflict.write_text(json.dumps(records[0]) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "identity conflict"):
            ingest(self.output, conflict)
        source.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "shard changed"):
            ingest(self.output, source)

    def test_cloud_collection_repairs_corrupt_local_cache(self):
        writer = self.writer(cloud=True)
        with Recorder(writer, task_id="006-demo/001-main").span("generate"):
            pass
        manifest = writer.flush()
        volume = Mock()
        volume.read_file.side_effect = lambda path: [(self.root / "cloud" / path.lstrip("/")).read_bytes()]
        first = collect_cloud(self.output, volume, manifest)
        self.assertEqual(first["errors"], 0)
        self.assertEqual(first["events"], 2)
        ref = manifest["paths"][0]
        target = self.output / ".telemetry/raw/cloud" / ref["path"].removeprefix("telemetry/")
        for data in (b"truncated", b"x" * ref["bytes"]):
            with self.subTest(size=len(data)):
                target.write_bytes(data)
                volume.read_file.reset_mock()
                result = collect_cloud(self.output, volume, manifest)
                self.assertEqual(result["errors"], 0)
                self.assertEqual(result["events"], 0)
                volume.read_file.assert_called_once_with("/" + ref["path"])
                self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), ref["sha256"])

    def test_writer_failure_and_backpressure_do_not_fail_business_operation(self):
        writer = self.writer()
        recorder = Recorder(writer)
        with patch("shared.telemetry.storage.atomic_write", side_effect=OSError("disk full")):
            with recorder.span("save"):
                value = "business succeeded"
            manifest = writer.flush()
        self.assertEqual(value, "business succeeded")
        self.assertGreater(manifest["errors"], 0)
        self.assertGreater(manifest["dropped"], 0)
        with patch.object(writer, "emit", side_effect=OSError("recorder unavailable")):
            with recorder.span("generate"):
                pass

    def test_blocked_writer_does_not_block_emit_and_flush_reports_incomplete(self):
        writer = self.writer()
        entered, release = threading.Event(), threading.Event()
        from shared.telemetry.storage import atomic_write
        def slow(*args):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release writer")
            return atomic_write(*args)
        with patch("shared.telemetry.storage.atomic_write", side_effect=slow):
            try:
                with Recorder(writer).span("register"):
                    pass
                self.assertTrue(entered.wait(1))
                with Recorder(writer).span("submit"):
                    pass
                self.assertFalse(writer.flush(timeout=0.01)["complete"])
            finally:
                release.set()
                self.assertTrue(writer.flush()["complete"])

    def test_cloud_collection_validates_paths_size_and_checksum(self):
        volume = Mock()
        bad = {"path": "telemetry/../../secret", "sha256": "0" * 64, "bytes": 10}
        self.assertEqual(collect_cloud(self.output, volume, {"paths": [bad]})["errors"], 1)
        volume.read_file.assert_not_called()
        bad["path"] = "telemetry/" + "a" * 32 + "/00000000.jsonl"
        volume.read_file.return_value = [b"too many bytes"]
        self.assertEqual(collect_cloud(self.output, volume, {"paths": [bad]})["errors"], 1)
        self.assertFalse(list((self.output / ".telemetry/raw/cloud").glob("**/*.jsonl")))

    def test_resource_peaks_use_samples_inside_stage_not_container_history(self):
        writer = self.writer()
        recorder = Recorder(writer, task_id="006-demo/001-main", container_id="container-1")
        recorder.resource({"ram_bytes": 900, "ram_peak_bytes": 9999, "cpu_seconds": 100})
        with recorder.span("generate"):
            recorder.resource({"ram_bytes": 20, "ram_peak_bytes": 9999, "cpu_seconds": 101,
                               "gpus": [{"used_mib": 10}]})
            recorder.resource({"ram_bytes": 30, "ram_peak_bytes": 9999, "cpu_seconds": 103,
                               "gpus": [{"used_mib": 12}]})
        writer.flush()
        resources = task_report(self.output, "006-demo/001-main")["stages"][0]["resources"]
        self.assertEqual(resources["sampled_ram_peak_bytes"], 30)
        self.assertEqual(resources["sampled_gpu_peak_mib"], 12)
        self.assertEqual(resources["sampled_cpu_seconds"], 2)

    def test_early_artifact_completion_reference_still_collects_later_telemetry(self):
        writer = self.writer(cloud=True)
        recorder = Recorder(writer, task_id="006-demo/001-main")
        early = writer.reference()
        volume = Mock()
        volume.read_file.side_effect = lambda path: [(self.root / "cloud" / path.lstrip("/")).read_bytes()]
        self.assertTrue(collect_cloud(self.output, volume, early)["pending"])
        with recorder.span("save"):
            pass
        writer.publish(early)
        collected = collect_cloud(self.output, volume, early)
        self.assertFalse(collected["pending"])
        self.assertEqual(collected["errors"], 0)
        self.assertEqual(collected["events"], 2)

    def test_boundary_sample_request_does_not_wait_for_resource_probe(self):
        from shared.telemetry.sampler import ResourceMonitor
        entered, release = threading.Event(), threading.Event()
        def probe():
            entered.set()
            release.wait(2)
            return {}
        monitor = ResourceMonitor()
        with patch("shared.telemetry.sampler.resource_sample", side_effect=probe):
            monitor.start()
            try:
                self.assertTrue(entered.wait(1))
                with ThreadPoolExecutor(max_workers=1) as pool:
                    self.assertIsNone(pool.submit(monitor.request).result(timeout=0.5))
            finally:
                release.set()
                monitor.stop()

    def test_missing_final_event_remains_unclosed_in_report(self):
        writer = self.writer()
        recorder = Recorder(writer, task_id="006-demo/001-main")
        recorder.emit(record_type="stage", stage="submit", status="running", reason=None,
                      span_id="interrupted", started_at=schema.utc_now(), duration_ms=None,
                      measurement="measured", attributes={})
        writer.flush()
        self.assertEqual(task_report(self.output, "006-demo/001-main")["unclosed_spans"], 1)

    def test_telemetry_context_does_not_change_inference_signature(self):
        from shared.tasks.task_spec import prepare
        from tests.test_model_service import SPEC
        item = {"id": "001", "prompt": "boat", "seed": 1}
        self.assertEqual(prepare(SPEC, item), prepare(SPEC, {**item, "_telemetry": {
            "task_id": "006-demo/001-main", "job_key": "image/001"}}))

    def test_local_collection_cli_never_contacts_modal(self):
        with patch.object(runner, "ROOT", self.root), \
                patch("sys.argv", ["orchestration", "telemetry", "collect", "--local"]), \
                patch("shared.serving.modal_setup.model_volume") as volume, \
                patch("modal.Cls.from_name") as model, patch("sys.stdout", new=io.StringIO()) as output:
            runner.main()
        volume.assert_not_called()
        model.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["errors"], 0)

    def test_background_collector_publishes_report(self):
        from shared.telemetry.collector import Collector
        from shared.tasks.batch import intent_digest
        writer = self.writer(cloud=True)
        with Recorder(writer, task_id="006-demo/001-main", job_key="image/001").span("generate"):
            pass
        manifest = writer.publish(writer.reference())
        volume = Mock()
        volume.read_file.side_effect = lambda path: [(self.root / "cloud" / path.lstrip("/")).read_bytes()]
        collector = Collector(self.output, volume)
        self.addCleanup(collector.close)
        collector.submit({**manifest, "task_id": "006-demo/001-main"})
        path = self.output / ".telemetry/reports" / (intent_digest("006-demo/001-main") + ".json")
        deadline = time.monotonic() + 3
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(path.exists())
        self.assertEqual(read_json(path)["stages"][0]["status"], "completed")

    def test_full_task_handoff_is_traceable_and_download_metrics_are_saved(self):
        shutil.copytree(runner.ROOT / "workflows", self.root / "workflows")
        task = self.root / "tasks/006-telemetry-demo.json"
        atomic_json(task, {"version": 1, "name": "006-telemetry-demo", "workflow": "storyboard-to-video",
                          "items": [{"id": "001", "first_prompt": "boat", "last_prompt": "lake",
                                     "motion_prompt": "drift"}]})
        directory = runner.new_run(runner.build_plan(task, root=self.root), root=self.root)
        cloud_root = self.root / "cloud"
        volumes = Mock()
        volumes.read_file.side_effect = lambda path: [(cloud_root / path.lstrip("/")).read_bytes()]
        recorders = {name: Recorder(self.writer(cloud=True), container_id=name) for name in ("moss", "pika")}
        for recorder in recorders.values():
            with recorder.span("startup", mode="post_snapshot_hook", initialization_is_snapshot_history=True):
                pass
            cpu = [0]
            def sample(recorder=recorder, cpu=cpu):
                cpu[0] += 1
                recorder.resource({"cpu_seconds": cpu[0], "ram_bytes": 1024, "gpus": [{"used_mib": 512}]})
            recorder.sampler = sample

        class Backend(sdk.Backend):
            def __init__(self):
                self.volume = volumes
                self.calls, self.results = {}, {}
            def fingerprint(self, spec):
                return "cache"
            def submit(self, spec, item, prefix, fingerprint):
                key = "fc-" + str(len(self.calls))
                self.calls[key] = (spec, item, prefix)
                return key
            def poll(self, record):
                call = record["call_id"]
                if call not in self.results:
                    spec, item, prefix = self.calls[call]
                    with patch("shared.serving.model_service.Path", wraps=Path) as paths, \
                            patch("shared.serving.inference_api.VLLMImageClient") as images, \
                            patch("shared.serving.inference_api.SGLangVideoClient") as videos:
                        paths.side_effect = lambda value: cloud_root if value == "/mnt/modal" else Path(value)
                        images.return_value.generate.return_value = (PNG, ".png")
                        videos.return_value.submit.return_value = "engine-video-1"
                        videos.return_value.content.return_value = MP4
                        self.results[call] = next(execute(spec, [item], prefix, self.volume, "cache",
                                                        recorder=recorders[spec["service"]]))
                return self.results[call]
        backend = Backend()
        with patch("shared.telemetry.collector.enqueue"):
            result = sdk.execute(directory, backend=backend, timeout=3, poll=0.001)
        self.assertEqual(result["counts"], {"completed": 3})
        flush_local(self.output)
        summary = collect(self.output, volumes)
        self.assertEqual(summary["errors"], 0)
        report = task_report(self.output, "006-telemetry-demo/001-main")
        stages = {span["stage"] for span in report["stages"]}
        self.assertEqual(stages, schema.STAGES)
        self.assertTrue(report["resource_samples"])
        self.assertEqual(report["unclosed_spans"], 0)
        self.assertIsNone(report["platform_wait_ms"])
        for job in report["jobs"].values():
            self.assertEqual(job["missing_stages"], [])
        video = [span for span in report["stages"] if span["job_key"] == "video/001" and span["stage"] == "generate"]
        self.assertTrue(any(span["engine_job_id"] == "engine-video-1" for span in video))
        self.assertTrue(all(span["call_id"] for span in video))
        downloads = [span for span in report["stages"] if span["stage"] == "download"]
        self.assertTrue(all(span["attributes"]["downloaded_bytes"] > 0 for span in downloads))
        self.assertEqual(collect(self.output, volumes)["events"], 0)

    def test_telemetry_publication_failure_preserves_completed_artifact(self):
        from tests.test_model_service import SPEC
        recorder = Recorder(self.writer(cloud=True), task_id="006-demo/001-main")
        volume = Mock()
        volume.commit.side_effect = [None, None, None, OSError("telemetry commit failed")]
        with patch("shared.serving.model_service.Path", wraps=Path) as paths, \
                patch("shared.serving.inference_api.VLLMImageClient") as images:
            paths.side_effect = lambda value: self.root / "cloud" if value == "/mnt/modal" else Path(value)
            images.return_value.generate.return_value = (PNG, ".png")
            result = next(execute(SPEC, [{"id": "001", "prompt": "boat", "seed": 1}],
                                  "model-results/test", volume, recorder=recorder))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["telemetry_error"], "OSError")
        self.assertTrue((self.root / "cloud/model-results/test/001.png").is_file())
