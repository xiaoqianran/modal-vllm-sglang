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
            "shared.serving.model_service", "shared.serving.inference_gateway", "shared.serving.modal_setup"
        }:
            raise AssertionError("Unexpected deployment dependency: " + fullname)
sys.meta_path.insert(0, NoDeployment())
import base64
import hashlib
import json
import tempfile
from pathlib import Path
from shared.tasks import job_client
from shared.utils.files import read_json, atomic_json, download_volume_file
from shared.tasks.task_spec import ROOT, prepare
for folder in ("001-moss-image", "002-pika-h3-flex", "005-minimax-h3-image"):
    spec = read_json(ROOT / folder / "model.json")
    payload, signature = prepare(spec, {"id": "001", "prompt": "boat", "seed": 42})
    expected = hashlib.sha256(json.dumps({"spec": spec, "operation": spec["operation"],
        "payload": payload}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    assert signature == expected
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    spec = read_json(ROOT / "002-pika-h3-flex/model.json")
    image = bytes.fromhex("89504e470d0a1a0a") + b"x" * 64
    (root / "frame.png").write_bytes(image)
    payload, _ = prepare(spec, {"id": "001", "operation": "fl2va", "prompt": "boat", "seed": 42,
        "first_frame_base64": base64.b64encode(image).decode(),
        "last_frame_base64": base64.b64encode(image).decode()})
    assert payload["first_frame_base64"] == base64.b64encode(image).decode()
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
