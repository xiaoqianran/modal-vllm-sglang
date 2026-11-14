"""Real local HTTP servers exercise admission independently of Modal/GPU."""
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from shared import inference_gateway
from shared.generation_gate import GenerationGate
from shared.http import request_bytes, request_json
from shared.task_lifecycle import SubmissionRejected, recovery_action


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.submissions = 0
        self.status = "running"
        self.submit_code = 200
        self.status_code = 200
        self.response = {"id": "job-1"}
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.addCleanup(self.release.set)
        case = self

        class Engine(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, body):
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                case.submissions += 1
                case.entered.set()
                case.release.wait(5)
                self.reply(case.submit_code, json.dumps(case.response).encode())

            def do_GET(self):
                if self.path.endswith("/content"):
                    self.reply(200, b"video-content")
                else:
                    self.reply(case.status_code, json.dumps({"status": case.status}).encode())

        class QuietHandler(inference_gateway.Handler):
            def log_message(self, *args):
                pass

        engine = self.start_server(Engine)
        self.spec = {"kind": "video", "timeout": 2}
        for name, value in (("SPEC", self.spec), ("UPSTREAM", engine)):
            context = patch.object(inference_gateway, name, value)
            context.start()
            self.addCleanup(context.stop)
        self.endpoint = self.start_server(QuietHandler)

    def start_server(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.generation_gate = GenerationGate()
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()

        def stop():
            self.release.set()
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.addCleanup(stop)
        return f"http://127.0.0.1:{server.server_port}"

    def submit(self):
        path = "/v1/videos" if self.spec["kind"] == "video" else "/v1/images/generations"
        return request_json(self.endpoint + path, {"prompt": "test"}, timeout=3)

    def test_video_holds_admission_after_post_and_reads_remain_available(self):
        self.assertEqual(self.submit(), {"id": "job-1"})
        with self.assertRaises(SubmissionRejected):
            self.submit()
        self.assertEqual(request_json(self.endpoint + "/v1/videos/job-1", timeout=1)["status"], "running")
        self.assertEqual(request_bytes(self.endpoint + "/v1/videos/job-1/content", timeout=1), b"video-content")
        self.assertEqual(self.submissions, 1)
        self.status = "completed"
        self.submit()
        self.assertEqual(self.submissions, 2)

    def test_inflight_image_blocks_other_post_but_not_health(self):
        self.spec["kind"] = "image"
        self.response = {"data": []}
        self.release.clear()
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(self.submit)
            try:
                self.assertTrue(self.entered.wait(1))
                with self.assertRaises(SubmissionRejected):
                    self.submit()
                self.assertEqual(request_json(self.endpoint + "/health", timeout=1)["status"], "running")
                self.assertEqual(self.submissions, 1)
            finally:
                self.release.set()
            running.result(3)
        self.submit()
        self.assertEqual(self.submissions, 2)

    def test_unknown_acceptance_never_releases_generation(self):
        self.response = {}  # Accepted request, lost/malformed job ID.
        with self.assertRaises(RuntimeError) as failure:
            self.submit()
        self.assertNotIsInstance(failure.exception, SubmissionRejected)
        self.status = "completed"  # Without an ID this cannot prove which job finished.
        with self.assertRaises(SubmissionRejected):
            self.submit()
        self.assertEqual(self.submissions, 1)

    def test_poll_failure_and_unknown_status_do_not_release_generation(self):
        self.submit()
        self.status_code = 503
        with self.assertRaises(SubmissionRejected):
            self.submit()
        self.status_code = 200
        self.status = "unrecognized"
        with self.assertRaises(SubmissionRejected):
            self.submit()
        self.assertEqual(self.submissions, 1)
        self.status = "failed"
        self.submit()
        self.assertEqual(self.submissions, 2)

    def test_server_error_does_not_imply_no_generation(self):
        self.submit_code = 500
        with self.assertRaises(RuntimeError):
            self.submit()
        with self.assertRaises(SubmissionRejected):
            self.submit()
        self.assertEqual(self.submissions, 1)

    def test_explicit_engine_rejection_releases_gate(self):
        self.submit_code = 422
        with self.assertRaises(RuntimeError):
            self.submit()
        self.submit_code = 200
        self.submit()
        self.assertEqual(self.submissions, 2)


class RecoveryTests(unittest.TestCase):
    def test_common_recovery_decisions(self):
        for status in ("pending", "blocked"):
            self.assertEqual(recovery_action({"status": status}), "submit")
        for status in ("submitting", "running", "collecting", "completed", "uncertain"):
            with self.subTest(status=status):
                self.assertEqual(recovery_action({"status": status}), "uncertain")
                self.assertEqual(recovery_action({"status": status, "job_id": "1"}), "resume")
        for status in ("failed", "cancelled"):
            self.assertEqual(recovery_action({"status": status, "job_id": "1"}), "stop")
