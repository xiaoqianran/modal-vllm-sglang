"""Task/client imports must work without deployment or gateway modules."""
import subprocess
import sys
import unittest
from pathlib import Path


class TaskSpecTests(unittest.TestCase):
    def test_task_preparation_and_download_do_not_import_deployment(self):
        script = '''
import importlib.abc
import sys
class NoDeployment(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "modal" or fullname.startswith("modal.") or fullname in {
            "shared.model_service", "shared.inference_gateway", "shared.modal_setup"
        }:
            raise AssertionError("Unexpected deployment dependency: " + fullname)
sys.meta_path.insert(0, NoDeployment())
import base64
import hashlib
import json
import tempfile
from pathlib import Path
from shared import job_client
from shared.files import read_json, atomic_json, download_volume_file
from shared.task_spec import ROOT, load_task, prepare, task_key
for folder in ("001-moss-image", "002-pika-h3-flex", "005-minimax-h3-image"):
    spec = read_json(ROOT / folder / "model.json")
    payload, signature = prepare(spec, {"id": "001", "prompt": "boat", "seed": 42})
    expected = hashlib.sha256(json.dumps({"spec": spec, "operation": spec["operation"],
        "payload": payload}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    assert signature == expected
    assert task_key("Demo", "001-main", "001", spec["kind"]) == "demo/001-main/001/" + spec["kind"]
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    spec = read_json(ROOT / "002-pika-h3-flex/model.json")
    image = bytes.fromhex("89504e470d0a1a0a") + b"x" * 64
    (root / "frame.png").write_bytes(image)
    atomic_json(root / "task.json", {"version": 1, "name": "demo", "workflow": "pika-fl2va",
        "items": [{"id": "001", "prompt": "boat", "first_frame": {"file": "frame.png"},
                   "last_frame": {"file": "frame.png"}}]})
    project, scene, operation, items = load_task(spec, "task.json", root=root)
    assert (project, scene, operation) == ("demo", "001-main", "fl2va")
    assert prepare(spec, items[0])[0]["first_frame_base64"] == base64.b64encode(image).decode()
    class Volume:
        def read_file(self, path):
            yield image
    download_volume_file(Volume(), "/image.png", root / "download.png", kind="image",
                         expected_sha256=hashlib.sha256(image).hexdigest())
'''
        result = subprocess.run([sys.executable, '-c', script],
                                cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_existing_task_imports_remain_compatible(self):
        from shared import model_service, task_spec
        self.assertIs(model_service.prepare, task_spec.prepare)
        self.assertIs(model_service.load_task, task_spec.load_task)
        self.assertEqual(model_service.ROOT, task_spec.ROOT)
