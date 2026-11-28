import io
import tempfile
import unittest

from fastapi.testclient import TestClient
from PIL import Image

from experiments.image_studio.studio import DiskStore, create_app, run_job, save_asset, asset_path, json_get


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

        seen, red, blue = self.seen, self.red, self.blue
        class FakeEngine:
            def render_asset(self, store, job):
                parent = job["parent_id"]
                original = store.read(asset_path(parent) + "/image") if parent else None
                seen.append((job["size"], parent, original))
                return save_asset(store, blue if parent else red, parent=parent, prompt=job["prompt"])
        self.engine = FakeEngine()
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
        self.assertEqual(self.seen[-1], ("2048x1152", None, None))
        for prompt in ("换黄伞", "换白伞"):
            child = self.submit(operation="edit", parent_id=root["id"], prompt=prompt)["asset"]
            self.assertEqual(child["parent_id"], root["id"])
            self.assertNotEqual(child["id"], root["id"])
            self.assertEqual(self.client.get("/assets/" + child["id"]).content, self.blue)
            self.assertEqual(self.seen[-1][1:], (root["id"], self.red))
        self.assertEqual(self.client.get("/assets/" + root["id"]).content, self.red)

    def test_upload_edit_and_engine_failure_preserve_input(self):
        uploaded = self.client.post("/uploads", content=png("green", (96, 64)), headers=self.headers)
        self.assertEqual(uploaded.status_code, 201)
        asset_id = uploaded.json()["id"]
        original = self.client.get("/assets/" + asset_id).content
        class FailedEngine:
            def render_asset(self, store, job):
                raise RuntimeError("private engine detail")
        self.engine = FailedEngine()
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


class SDKStudioTest(unittest.TestCase):
    def test_private_sdk_reuses_cloud_asset_and_recovers_without_resubmit(self):
        from unittest.mock import Mock, patch
        from experiments.image_studio.studio import SDKH3, json_get, json_put, asset_path
        from runtime.tasks.task_spec import prepare, execution_signature
        from runtime.tasks.task_identity import intent_digest
        from runtime.config.storage import volume_key
        from orchestration.sdk import model_specs
        import hashlib
        import time
        blob = png("red")
        with tempfile.TemporaryDirectory() as directory:
            store = DiskStore(directory)
            backend = Mock()
            backend.fingerprint.return_value = "cache"
            spec = model_specs()["minimax"]
            backend.modal.Function.from_name.return_value.remote.return_value = intent_digest(["source", spec, "cache"])
            backend.preflight_result.return_value = None
            files, results = {}, {}
            backend.volume.read_file.side_effect = lambda path: iter([files[path]])
            def submit(spec, item, prefix, fingerprint, **kwargs):
                identity = "fc-" + item["id"]
                signature = prepare(spec, item)[1]
                filename = item["id"] + ".png"
                files[volume_key("/" + prefix + "/" + filename)] = blob
                results[identity] = {"id": item["id"], "file": filename, "status": "completed",
                                     "sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob),
                                     "intent_hash": signature,
                                     "execution_hash": execution_signature(signature, fingerprint)}
                return identity
            backend.submit.side_effect = submit
            backend.poll.side_effect = lambda record: results[record["call_id"]]
            engine = SDKH3(backend)
            first = {"id": "a"*32, "parent_id": None, "prompt": "mountain",
                     "size": "2048x1152", "seed": 1, "status": "queued", "submitted_at": time.time()}
            second = {**first, "id": "b"*32, "parent_id": first["id"], "prompt": "snow"}
            with patch("orchestration.deployment_state.model_source_version", return_value="source"):
                for job in (first, second):
                    json_put(store, f"jobs/{job['id']}.json", job)
                    run_job(store, engine, job)
                    self.assertEqual(json_get(store, f"jobs/{job['id']}.json")["status"], "ready")
                first_asset = json_get(store, asset_path(first["id"]) + "/meta.json")
                second_asset = json_get(store, asset_path(second["id"]) + "/meta.json")
                self.assertEqual(backend.submit.call_args.args[1]["image_artifact"], first_asset["artifact"])
                self.assertNotEqual(first_asset["artifact"]["path"], second_asset["artifact"]["path"])
                self.assertFalse((store.root / asset_path(first["id"]) / "image").exists())
                self.assertEqual(backend.submit.call_count, 2)
                key = f"jobs/{second['id']}.json"
                json_put(store, key, {**json_get(store, key), "status": "failed"})
                run_job(store, engine, second)
                self.assertEqual(json_get(store, key)["status"], "ready")
                self.assertEqual(backend.submit.call_count, 2)
                backend.register_delivery.assert_not_called()


if __name__ == "__main__":
    unittest.main()
