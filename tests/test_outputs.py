"""Output layout, explicit download selection and lossless historical import."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shared import outputs
from shared.files import atomic_json, read_json, sha256_file
from scripts.organize_outputs import inventory, apply
from scripts.migrate_scene_outputs import migrate as migrate_scenes


class OutputTests(unittest.TestCase):
    def test_migrate_existing_run_folders_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / "outputs"
            source = base / "002-again-love-opening" / "20261108T120000Z"
            source.mkdir(parents=True)
            (source / "001.png").write_bytes(b"sample-original-image")
            (source / "direct-modal-report.json").write_text('{"status":"ok"}')
            preview = migrate_scenes(base)
            self.assertEqual((preview["runs"], preview["media"]), (1, 1))
            result = migrate_scenes(base, apply=True)
            dest = base / "002-again-love" / "001-hotel-corridor" / "001.png"
            self.assertEqual(result["media"], 1)
            self.assertEqual(dest.read_bytes(), b"sample-original-image")
            self.assertFalse(source.exists())
            archive = base / "_migration" / "_archived_runs" / "002-again-love-opening" / "20261108T120000Z"
            self.assertTrue((archive / "direct-modal-report.json").exists())
            self.assertEqual(migrate_scenes(base)["runs"], 0)

    def test_scene_migration_rejects_filename_collision(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / "outputs"
            source = base / "002-again-love-opening" / "20261108T120000Z"
            source.mkdir(parents=True)
            (source / "001.png").write_bytes(b"old")
            dest = base / "002-again-love" / "001-hotel-corridor"
            dest.mkdir(parents=True)
            (dest / "001.png").write_bytes(b"different")
            with self.assertRaises(FileExistsError):
                migrate_scenes(base, apply=True)
            self.assertEqual((source / "001.png").read_bytes(), b"old")
            self.assertEqual((dest / "001.png").read_bytes(), b"different")

    def test_scene_directory_is_stable_and_step_metadata_hidden(self):
        with tempfile.TemporaryDirectory() as temp:
            first = outputs.run_dir("demo", root=temp)
            second = outputs.run_dir("demo", root=temp)
            self.assertEqual(first, second)
            self.assertEqual(first.relative_to(temp).as_posix(), "demo/001-main")
            path = first / "001.png"
            path.write_bytes(b"image")
            artifact = outputs.record_artifact(first, path, "image")
            self.assertEqual(artifact["path"], "001.png")
            self.assertTrue((first / ".runner/items/001/image/artifacts.json").is_file())
            self.assertEqual(artifact["sha256"], sha256_file(path))
            for name in ("..", "a/b", "CON"):
                with self.assertRaises(ValueError):
                    outputs.run_dir(name, root=temp)

    def make_legacy(self, root):
        source = root / "001-moss-image/results/storyboard/episode09/20261108T120000Z"
        source.mkdir(parents=True)
        (source / "09-01.png").write_bytes(b"legacy-image")
        atomic_json(source / "report.json", {
            "output_prefix": "008-moss-storyboard/episode09/20261108T120000Z",
            "shots": [{"id": "09-01", "file": "09-01.png", "status": "ok"}]})
        return source

    def test_lossless_idempotent_import_and_provenance(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.make_legacy(root)
            plan = inventory(root)
            self.assertFalse((root / "outputs").exists())
            journal = apply(plan, root)
            self.assertEqual(read_json(journal)["files_verified"], 2)
            run = root / "outputs/episode09/legacy-20261108T120000Z"
            self.assertEqual(sha256_file(source / "09-01.png"), sha256_file(run / "items/09-01/image/image.png"))
            self.assertEqual(sha256_file(source / "report.json"), sha256_file(run / "source-report.json"))
            report = read_json(run / "report.json")
            self.assertFalse(report["resume_supported"])
            self.assertEqual(report["shots"][0]["local_path"], "items/09-01/image/image.png")
            apply(plan, root)
            self.assertTrue((source / "09-01.png").exists())

    def test_changed_source_and_conflicting_destination_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.make_legacy(root)
            plan = inventory(root)
            (source / "09-01.png").write_bytes(b"changed")
            with self.assertRaises(ValueError):
                apply(plan, root)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_legacy(root)
            plan = inventory(root)
            destination = root / plan["files"][0]["destination"]
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"existing-different-file")
            with self.assertRaises(ValueError):
                apply(plan, root)
            self.assertEqual(destination.read_bytes(), b"existing-different-file")

    def test_benchmarks_and_unknown_files_separated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "001-moss-image/results/sglang/20261108T120000Z"
            source.mkdir(parents=True)
            (source / "07-01.jpg").write_bytes(b"sample")
            unknown = root / "002-pika-h3-flex/results/scratch/request.json"
            unknown.parent.mkdir(parents=True)
            unknown.write_text("{}")
            plan = inventory(root)
            destinations = [e["destination"] for e in plan["files"]]
            self.assertIn("benchmarks/moss-sglang/legacy-20261108T120000Z/samples/07-01.jpg", destinations)
            self.assertIn("outputs/_legacy/pika/scratch/request.json", destinations)

    def test_storyboard_download_uses_selected_report_and_new_layout(self):
        spec = importlib.util.spec_from_file_location("download_storyboard", outputs.REPO / "001-moss-image/download_storyboard.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        class Volume:
            def read_file(self, path):
                self_path.append(path)
                yield b"downloaded-image"

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.make_legacy(root)
            self_path = []
            with patch.object(outputs, "REPO", root):
                module.download(source / "report.json", Volume())
                new_report = root / "outputs/episode09/001-main/.runner/moss-batch/report.json"
                module.download(new_report, Volume())
            self.assertEqual(self_path, ["/008-moss-storyboard/episode09/20261108T120000Z/09-01.png"])
            self.assertTrue((root / "outputs/episode09/001-main/09-01.png").exists())


if __name__ == "__main__":
    unittest.main()
