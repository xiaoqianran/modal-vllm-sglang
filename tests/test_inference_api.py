"""Engine API protocol tests; all requests are mocked, no GPU required."""
import base64
import unittest
from unittest.mock import patch

from shared.inference_api import SGLangVideoClient, VLLMImageClient


PNG = b"\x89PNG\r\n\x1a\n" + b"minimal-test-image"
JPEG = b"\xff\xd8\xff" + b"minimal-test-image"
MP4 = b"\x00\x00\x00\x1cftypisom" + b"fake-mp4-contents" * 3


class ImageClientTests(unittest.TestCase):
    def test_optional_auth_headers_forwarded(self):
        headers = {"Modal-Key": "test-key", "Modal-Secret": "test-secret"}
        body = {"data": [{"b64_json": base64.b64encode(PNG).decode()}]}
        with patch("shared.inference_api.request_json", return_value=body) as call:
            VLLMImageClient("http://localhost", headers=headers).generate({"prompt": "Test"})
            self.assertEqual(call.call_args.kwargs["headers"], headers)
        video = SGLangVideoClient("http://localhost", headers=headers)
        with patch("shared.inference_api.request_json", return_value={"id": "job"}) as call:
            video.submit({"prompt": "Test"})
            self.assertEqual(call.call_args.kwargs["headers"], headers)
            video.status("job")
            self.assertEqual(call.call_args.kwargs["headers"], headers)
        with patch("shared.inference_api.request_bytes", return_value=MP4) as call:
            video.content("job")
            self.assertEqual(call.call_args.kwargs["headers"], headers)

    def test_png_and_jpeg(self):
        client = VLLMImageClient("http://localhost:8000/")
        for raw, extension in ((PNG, ".png"), (JPEG, ".jpg")):
            body = {"data": [{"b64_json": base64.b64encode(raw).decode()}]}
            with self.subTest(extension=extension), \
                 patch("shared.inference_api.request_json", return_value=body) as call:
                result = client.generate({"prompt": "Example", "size": "1024x1024"},
                                         timeout=60)
                self.assertEqual(result, (raw, extension))
                call.assert_called_once_with(
                    "http://localhost:8000/v1/images/generations",
                    {"prompt": "Example", "size": "1024x1024"}, timeout=60)

    def test_invalid_responses_rejected(self):
        client = VLLMImageClient("http://localhost")
        for body in ({"data": []}, {"data": [{}]},
                     {"data": [{"b64_json": "not valid==="}]},
                     {"data": [{"b64_json": base64.b64encode(b"bad").decode()}]}):
            with self.subTest(body=body), \
                 patch("shared.inference_api.request_json", return_value=body):
                with self.assertRaises(ValueError):
                    client.generate({"prompt": "Test"})


class VideoClientTests(unittest.TestCase):
    def test_submit_wait_content_and_generate(self):
        client = SGLangVideoClient("http://localhost:30011/")
        calls = []
        polls = []
        responses = iter(({"status": "queued"}, {"status": "completed"}))

        def fake_json(url, payload=None, **options):
            calls.append((url, payload, options))
            if url.endswith("/v1/videos"):
                return {"id": "job-01"}
            return next(responses)

        with patch("shared.inference_api.request_json", side_effect=fake_json), \
             patch("shared.inference_api.request_bytes", return_value=MP4) as content, \
             patch("shared.http.time.sleep"):
            video_id = client.submit({"prompt": "Rain"}, timeout=300)
            state = client.wait(video_id, timeout=10, poll=1,
                                on_status=lambda item: polls.append(item["status"]))
            self.assertEqual(state["status"], "completed")
            self.assertEqual(polls, ["queued", "completed"])
            self.assertEqual(client.content(video_id, timeout=100), MP4)
            content.assert_called_once_with(
                "http://localhost:30011/v1/videos/job-01/content",
                timeout=100, attempts=2)
        self.assertEqual(video_id, "job-01")
        self.assertEqual(calls[0], ("http://localhost:30011/v1/videos",
                                    {"prompt": "Rain"}, {"timeout": 300}))
        self.assertEqual(calls[1][0], "http://localhost:30011/v1/videos/job-01")

    def test_failed_job_and_invalid_video(self):
        client = SGLangVideoClient("http://localhost")
        with patch("shared.inference_api.request_json",
                   return_value={"status": "failed", "error": "OOM"}):
            with self.assertRaisesRegex(RuntimeError, "failed"):
                client.wait("job", timeout=10)
        with patch("shared.inference_api.request_bytes", return_value=b"bad"):
            with self.assertRaisesRegex(ValueError, "Invalid MP4"):
                client.content("job")
        with patch("shared.inference_api.request_json", return_value={}):
            with self.assertRaisesRegex(ValueError, "no id"):
                client.submit({"prompt": "Test"})

    def test_generate_convenience_and_encoded_job_id(self):
        client = SGLangVideoClient("http://localhost")
        with patch.object(client, "submit", return_value="job"), \
             patch.object(client, "wait") as wait, \
             patch.object(client, "content", return_value=MP4) as content:
            self.assertEqual(client.generate({"prompt": "A"}, timeout=20, poll=1),
                             ("job", MP4))
            wait.assert_called_once_with(
                "job", timeout=20, poll=1, status_timeout=180)
            content.assert_called_once_with("job", timeout=600)
        with patch("shared.inference_api.request_json",
                   return_value={"status": "completed"}) as fetch:
            client.status("a/b?c")
            self.assertEqual(
                fetch.call_args.args[0],
                "http://localhost/v1/videos/a%2Fb%3Fc")


if __name__ == "__main__":
    unittest.main()
