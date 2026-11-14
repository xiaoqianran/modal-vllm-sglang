"""Deploy-only contract: CPU downloads are the only standalone Modal Run tools."""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_FILES = (
    "001-moss-image/app.py",
    "001-moss-image/image.py",
    "001-moss-image/run.py",
    "002-pika-h3-flex/app.py",
    "002-pika-h3-flex/run.py",
    "002-pika-h3-flex/video.py",
    "005-minimax-h3-image/app.py",
    "005-minimax-h3-image/image.py",
    "005-minimax-h3-image/run.py",
)


class DeployOnlyTests(unittest.TestCase):
    def test_minimax_one_class_serves_http_and_sdk(self):
        tree = ast.parse((ROOT / "005-minimax-h3-image/app.py").read_text(encoding="utf-8"))
        cls_nodes = [node for node in tree.body if isinstance(node, ast.ClassDef)
                     and any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                             and d.func.attr == "cls" for d in node.decorator_list)]
        self.assertEqual(len(cls_nodes), 1)
        self.assertEqual(cls_nodes[0].name, "H3")
        cls_deco = next(d for d in cls_nodes[0].decorator_list
                        if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                        and d.func.attr == "cls")
        self.assertTrue(any(k.arg == "max_containers" and isinstance(k.value, ast.Constant)
                            and k.value.value == 1 for k in cls_deco.keywords))
        self.assertFalse(any(
            isinstance(node, ast.FunctionDef)
            and any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                    and d.func.attr == "function" for d in node.decorator_list)
            for node in tree.body
        ))
        methods = {node.name: node for node in cls_nodes[0].body
                   if isinstance(node, ast.FunctionDef)}
        for name, decorator in (("serve", "web_server"), ("generate_batch", "method")):
            self.assertTrue(any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                                and d.func.attr == decorator for d in methods[name].decorator_list))
        self.assertNotIn("subprocess.Popen",
                         (ROOT / "005-minimax-h3-image/image.py").read_text(encoding="utf-8"))

    def test_pika_single_gpu_class(self):
        tree = ast.parse((ROOT / "002-pika-h3-flex/app.py").read_text(encoding="utf-8"))
        classes = [n for n in tree.body if isinstance(n, ast.ClassDef)
                   and any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                           and d.func.attr == "cls" for d in n.decorator_list)]
        self.assertEqual(len(classes), 1)
        self.assertEqual(classes[0].name, "Pika")
        methods = {n.name: n for n in classes[0].body if isinstance(n, ast.FunctionDef)}
        self.assertTrue({"start", "serve", "generate_batch", "stop"} <= methods.keys())
        cls = next(d for d in classes[0].decorator_list if isinstance(d, ast.Call)
                   and isinstance(d.func, ast.Attribute) and d.func.attr == "cls")
        self.assertTrue(any(k.arg == "max_containers" and isinstance(k.value, ast.Constant)
                            and k.value.value == 1 for k in cls.keywords))
        self.assertFalse(any(
            isinstance(n, ast.FunctionDef) and any(
                isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                and d.func.attr == "function" for d in n.decorator_list)
            for n in tree.body
        ))
        self.assertNotIn("subprocess.Popen", (ROOT / "002-pika-h3-flex/video.py").read_text())

    def test_moss_has_one_shared_gpu_class_for_http_and_batch(self):
        tree = ast.parse((ROOT / "001-moss-image/app.py").read_text(encoding="utf-8"))
        gpu_classes = [node for node in tree.body
                       if isinstance(node, ast.ClassDef)
                       and any(isinstance(d, ast.Call)
                               and isinstance(d.func, ast.Attribute)
                               and d.func.attr == "cls" for d in node.decorator_list)]
        self.assertEqual(len(gpu_classes), 1)
        self.assertEqual(gpu_classes[0].name, "Moss")
        self.assertFalse(any(
            isinstance(node, ast.FunctionDef)
            and any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                    and d.func.attr == "function" for d in node.decorator_list)
            for node in tree.body
        ))
        cls_args = next(d for d in gpu_classes[0].decorator_list
                        if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                        and d.func.attr == "cls")
        self.assertTrue(any(k.arg == "max_containers" and isinstance(k.value, ast.Constant)
                            and k.value.value == 1 for k in cls_args.keywords))
        methods = {node.name: node for node in gpu_classes[0].body
                   if isinstance(node, ast.FunctionDef)}
        self.assertTrue({"start", "stop", "serve", "generate_batch_stream"} <= methods.keys())
        self.assertEqual(sum(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "Popen" for node in ast.walk(tree)
        ), 1)
        batch_source = (ROOT / "001-moss-image/image.py").read_text(encoding="utf-8")
        self.assertNotIn("subprocess.Popen", batch_source)
        for method_name, kind in (("serve", "web_server"),
                                  ("generate_batch_stream", "method")):
            self.assertTrue(any(
                isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                and d.func.attr == kind for d in methods[method_name].decorator_list
            ))

    def test_gpu_entrypoints_cannot_launch_temporary_modal_apps(self):
        for filename in RUNTIME_FILES:
            with self.subTest(filename=filename):
                contents = (ROOT / filename).read_text(encoding="utf-8")
                self.assertNotIn("app.run(", contents)
                self.assertNotIn(".run(detach=", contents)
                self.assertNotIn("@app.local_entrypoint", contents)
                tree = ast.parse(contents)
                self.assertFalse(any(
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "run"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "app"
                    for node in ast.walk(tree)
                ))

    def test_standalone_modal_run_entrypoints_are_cpu_preparation_only(self):
        source = tuple((ROOT / name).glob("*.py") for name in (
            "001-moss-image", "002-pika-h3-flex", "005-minimax-h3-image"
        ))
        files = (p for paths in source for p in paths if ".venv" not in p.parts)
        entrypoints = [
            p.relative_to(ROOT).as_posix() for p in files
            if "@app.local_entrypoint" in p.read_text(encoding="utf-8")
        ]
        self.assertEqual(set(entrypoints), {
            "001-moss-image/download_model.py",
            "002-pika-h3-flex/download_model.py",
            "005-minimax-h3-image/download_model.py",
        })


if __name__ == "__main__":
    unittest.main()
