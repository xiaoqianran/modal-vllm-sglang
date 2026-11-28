import shutil
import tempfile
import unittest
from pathlib import Path
import copy

from pipeline import s01_build_plan as runner
from orchestration import state
from orchestration.monitor import render
from orchestration.monitor_data import build_snapshot
from runtime.tasks.task_identity import intent_digest
from runtime.telemetry.recorder import close_local
from runtime.utils.files import read_json


class MonitorDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(close_local, self.root / ".runtime/logs")
        shutil.copytree(runner.ROOT / "workflows", self.root / "workflows")
        task = self.root / "tasks/008-dashboard.json"
        state.save(task, {"version": 1, "name": task.stem, "workflow": "storyboard-to-video",
                          "items": [{"id": "001", "first_prompt": "lake", "last_prompt": "shore", "motion_prompt": "drift"}]})
        self.plan = runner.build_plan(task, root=self.root)
        self.directory = runner.new_run(self.plan, root=self.root)
        self.current = read_json(self.directory / "state.json")

    def span(self, key, stage, start, end, duration, **attributes):
        return {"task_id": "008-dashboard/001-main", "job_key": key, "stage": stage, "status": "completed",
                "started_at": f"2026-01-01T00:00:{start:02}+00:00", "finished_at": f"2026-01-01T00:00:{end:02}+00:00",
                "duration_ms": duration, "measurement": "measured", "attributes": attributes}

    def complete(self):
        self.current.update(registered_at="2026-01-01T00:00:00+00:00", finished_at="2026-01-01T00:00:35+00:00")
        for job in self.plan["jobs"]:
            kind = "video" if job["step"] == "video" else "image"
            name = f"001_{job['step']}." + ("mp4" if kind == "video" else "png")
            (state.media_directory(self.directory) / name).write_bytes(b"test")
            self.current["jobs"][job["key"]] = {"status": "completed", "local_artifact": {"path": name, "type": kind}}

    def test_task_status_uses_controller_evidence_without_resubmitting(self):
        def snapshot():
            return build_snapshot(self.directory, self.plan, self.current, lambda _: None)

        self.assertEqual(snapshot()["status"], "pending")
        receipt = {"status": "queued", "call_id": "fc-legacy"}
        self.current["cloud_runs"] = {"001": receipt}
        before = copy.deepcopy(self.current)
        self.assertEqual(snapshot()["status"], "uncertain")
        self.assertEqual(self.current, before)  # Display only, never rewrite history.

        receipt.update(durable_plan=True)
        self.assertEqual(snapshot()["status"], "queued")
        receipt.update(status="running", controller_state="active")
        self.assertEqual(snapshot()["status"], "running")
        receipt.update(status="uncertain", controller_state="terminal")
        self.current["jobs"]["first/001"].update(status="queued", call_id="fc-gpu")
        self.assertEqual(snapshot()["status"], "uncertain")

    def test_task_status_distinguishes_queued_delivery_and_terminal(self):
        def snapshot():
            return build_snapshot(self.directory, self.plan, self.current, lambda _: None)

        self.current["jobs"]["first/001"].update(status="queued", call_id="fc-direct")
        self.assertEqual(snapshot()["status"], "queued")
        self.complete()
        # Terminal jobs take precedence over a stale controller receipt.
        self.current["cloud_runs"] = {"001": {"status": "uncertain", "controller_state": "terminal"}}
        self.assertEqual(snapshot()["status"], "completed")
        self.current["jobs"]["first/001"].pop("local_artifact")
        self.assertEqual(snapshot()["status"], "delivering")
        self.current["jobs"]["first/001"]["status"] = "failed"
        self.assertEqual(snapshot()["status"], "failed")

    def test_parallel_stage_totals_do_not_become_wall_time_and_reuse_is_excluded(self):
        self.complete()
        spans = [self.span("first/001", "generate", 10, 20, 10000), self.span("last/001", "generate", 10, 20, 10000),
                 self.span("video/001", "generate", 20, 30, 10000), self.span("first/001", "save", 10, 20, 10000),
                 self.span("first/001", "download", 31, 32, 1000, downloaded_bytes=1048576),
                 self.span("first/001", "download", 58, 59, 1000, downloaded_bytes=1048576, reused=True)]
        shared = self.span(None, "startup", 0, 10, 10000)
        shared["task_id"] = None
        spans.append(shared)
        state.save(self.root / ".runtime/logs/.telemetry/reports" / (intent_digest("008-dashboard/001-main") + ".json"),
                   {"task_id": "008-dashboard/001-main", "stages": spans})
        data = build_snapshot(self.directory, self.plan, self.current, lambda _: (864, 480))
        self.assertEqual(data["elapsed_ms"], 35000)
        self.assertGreater(sum(s["duration_ms"] or 0 for s in data["stages"]), data["elapsed_ms"])
        first = data["jobs"][0]
        self.assertEqual(first["transferred_bytes"], 1048576)
        self.assertEqual(first["download_bps"], 1048576)
        self.assertIsNone(next(s for s in first["stages"] if s["stage"] == "wait")["duration_ms"])
        self.assertIsNone(next(s for s in first["stages"] if s["stage"] == "startup")["duration_ms"])
        self.assertEqual(next(s for s in data["shared_startup"] if s["stage"] == "startup")["duration_ms"], 10000)

    def test_no_telemetry_is_unknown_and_payload_cannot_break_out_of_script(self):
        data = build_snapshot(self.directory, self.plan, self.current, lambda _: None)
        self.assertTrue(all(s["duration_ms"] is None for s in data["stages"]))
        self.plan["jobs"][0]["inputs"]["prompt"] = "</script><script>alert(1)</script>"
        state.save(self.directory / "plan.json", self.plan)
        render(self.root / "outputs")
        feed = (self.root / ".runtime/monitor/008-dashboard.js").read_text(encoding="utf-8")
        self.assertNotIn("</script><script>alert(1)", feed)
        self.assertIn("\\u003c/script>", feed)
        self.assertIn("window.updateTaskMonitor", feed)
        self.assertTrue((self.root / "outputs/index.html").is_file())
        self.assertFalse((self.root / "outputs/监控008-dashboard.html").exists())

    def test_image_default_is_one_k_and_explicit_video_workflow_is_preserved(self):
        task = self.root / "tasks/008-default-image.json"
        state.save(task, {"version": 1, "name": task.stem, "workflow": "moss-t2i", "items": [{"id": "001", "prompt": "lake"}]})
        plan = runner.build_plan(task, root=self.root)
        self.assertEqual(plan["render"], {"aspect_ratio": "1:1", "resolution": "1k"})
        self.assertEqual(plan["jobs"][0]["params"]["size"], "1024x1024")
        self.assertEqual(self.plan["jobs"][-1]["params"]["target"]["short_edge"], 480)
        task_data = read_json(task)
        task_data["render"] = {"aspect_ratio": "16:9"}
        state.save(task, task_data)
        self.assertEqual(runner.build_plan(task, root=self.root)["jobs"][0]["params"]["size"], "1024x576")

    def test_appending_shots_clears_previous_task_completion_boundary(self):
        self.complete()
        self.current["jobs"]["first/001"]["call_id"] = "fc-existing"
        state.save(self.directory / "state.json", self.current)
        task_path = self.root / "tasks/008-dashboard.json"
        task = read_json(task_path)
        task["items"].append({"id": "002", "first_prompt": "new lake", "last_prompt": "new shore", "motion_prompt": "new drift"})
        state.save(task_path, task)
        runner.new_run(runner.build_plan(task_path, root=self.root), root=self.root)
        updated = read_json(self.directory / "state.json")
        self.assertNotIn("finished_at", updated)
        self.assertEqual(updated["registered_at"], self.current["registered_at"])
        self.assertEqual(updated["jobs"]["first/001"]["call_id"], "fc-existing")

    def test_new_default_label_resumes_an_identical_legacy_image_plan(self):
        task = self.root / "tasks/008-legacy-image.json"
        state.save(task, {"version": 1, "name": task.stem, "workflow": "moss-t2i", "items": [{"id": "001", "prompt": "lake"}]})
        plan = runner.build_plan(task, root=self.root)
        old = copy.deepcopy(plan)
        old.pop("render")
        old.pop("hash")
        old["hash"] = intent_digest(old)
        directory = runner._new_run(old, root=self.root, output=".runtime/tasks")
        current = read_json(directory / "state.json")
        current["jobs"]["image/001"].update(status="queued", call_id="fc-original")
        state.save(directory / "state.json", current)
        self.assertEqual(runner.new_run(plan, root=self.root), directory)
        self.assertEqual(read_json(directory / "plan.json")["hash"], old["hash"])
        self.assertEqual(read_json(directory / "state.json")["jobs"]["image/001"]["call_id"], "fc-original")
