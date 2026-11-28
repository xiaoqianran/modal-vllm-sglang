import base64
import io
import tempfile
import unittest

from fastapi.testclient import TestClient
import httpx
from PIL import Image

from experiments.image_studio.studio import DiskStore, H3, create_app, run_job


def png(color, size=(64, 64)):
    mem = io.BytesIO()
    Image.new("RGB", size, color).save(mem, "PNG")
    return mem.getvalue()


class StudioTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = DiskStore(tmp.name)
        self.jobs = []
        self.seen = []
        self.red, self.blue = png("red"), png("blue")

        def serve(req):
            raw = req.read()
            self.seen.append((req.url.path, req.headers["content-type"], raw))
            blob = self.blue if req.url.path.endswith("/edits") else self.red
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(blob).decode()}]})

        self.engine = H3("https://engine.test", transport=httpx.MockTransport(serve))
        self.client = TestClient(create_app(self.store, self.jobs.append, "local-test"))
        self.client.auth = ("studio", "local-test")
        self.headers = {"X-Studio-Request": "1"}

    def submit(self, **payload):
        r = self.client.post("/jobs", json=payload, headers=self.headers)
        self.assertEqual(r.status_code, 202, r.text)
        job = r.json()
        self.assertEqual(self.client.get("/jobs/" + job["id"]).json()["status"], "queued")
        run_job(self.store, self.engine, self.jobs.pop(0))
        return self.client.get("/jobs/" + job["id"]).json()

    def test_generate_then_edit_and_branch_without_overwriting(self):
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn("图像工作台", home.text)
        root = self.submit(operation="generate", aspect="16:9", prompt="雨夜")["asset"]
        self.assertEqual(self.seen[-1][0], "/v1/images/generations")
        self.assertIn(b"2048x1152", self.seen[-1][2])
        for prompt in ("换黄伞", "换白伞"):
            child = self.submit(operation="edit", parent_id=root["id"], prompt=prompt)["asset"]
            self.assertEqual(child["parent_id"], root["id"])
            self.assertNotEqual(child["id"], root["id"])
            self.assertEqual(self.client.get("/assets/" + child["id"]).content, self.blue)
            path, mime, raw = self.seen[-1]
            self.assertEqual(path, "/v1/images/edits")
            self.assertTrue(mime.startswith("multipart/form-data; boundary="))
            self.assertIn(b"<Picture 1>", raw)
            self.assertIn(self.red, raw)
        self.assertEqual(self.client.get("/assets/" + root["id"]).content, self.red)

    def test_upload_edit_and_engine_failure_preserve_input(self):
        uploaded = self.client.post("/uploads", content=png("green", (96, 64)), headers=self.headers)
        self.assertEqual(uploaded.status_code, 201)
        asset_id = uploaded.json()["id"]
        original = self.client.get("/assets/" + asset_id).content
        self.engine = H3("https://engine.test", transport=httpx.MockTransport(
            lambda _: httpx.Response(502, text="private engine detail")))
        result = self.submit(operation="edit", prompt="加光", parent_id=asset_id)
        self.assertEqual(result["size"], "96x64")
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("private engine detail", str(result))
        self.assertEqual(self.client.get("/assets/" + asset_id).content, original)

    def test_private_endpoints_and_bounds(self):
        public = TestClient(self.client.app)
        self.assertEqual(public.get("/").status_code, 401)
        self.assertEqual(public.get("/assets/" + "a" * 32).status_code, 401)
        self.assertEqual(self.client.post("/jobs", json={}).status_code, 403)
        self.assertEqual(self.client.post("/uploads", content=b"not an image", headers=self.headers).status_code, 400)
        self.assertEqual(self.client.post("/jobs", content=b"x" * 32769, headers=self.headers).status_code, 413)
        self.assertEqual(self.client.post("/jobs", json={"operation": "edit", "prompt": "a",
                             "parent_id": "../escape"}, headers=self.headers).status_code, 404)
        self.assertFalse(self.jobs)


if __name__ == "__main__":
    unittest.main()
