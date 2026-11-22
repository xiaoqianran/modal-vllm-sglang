"""Scaffold a standard Modal image/video service without editing shared code."""
import argparse
import json
import re
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from runtime.utils.outputs import validate_path_component


def scaffold(directory, service, engine, kind, model, revision, image):
    validate_path_component(directory)
    validate_path_component(service)
    if engine not in {"vllm", "sglang"} or kind not in {"image", "video"}:
        raise ValueError("Expected vllm/sglang and image/video")
    target = ROOT / 'models' / directory
    if target.exists():
        raise FileExistsError(target)
    command = (["vllm", "serve", "{model_path}", "--served-model-name", "{model}", "--omni", "--enforce-eager"] if engine == "vllm"
               else ["sglang", "serve", "--model-path", "{model_path}"])
    command += ["--host", "0.0.0.0", "--port", "{port}"]
    template = Path(__file__).resolve().parent.parent / "templates/standard-model/model.json"
    spec = json.loads(template.read_text(encoding="utf-8"))
    spec.update(service=service, operation="t2i" if kind == "image" else "t2v",
                kind=kind, model=model, revision=revision, image=image, command=command,
                defaults={"response_format": "b64_json"} if kind == "image" else {})
    from runtime.config.model_spec import validate, discover
    validate(spec)
    if any(existing["service"] == service for existing in discover(ROOT).values()):
        raise ValueError(f"Service already exists: {service}")
    workflow_id = service + "-" + spec["operation"]
    workflow_path = ROOT / "workflows" / (workflow_id + ".json")
    if workflow_path.exists():
        raise FileExistsError(workflow_path)
    target.mkdir(parents=True)
    (target / "model.json").write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    bootstrap = 'from pathlib import Path\nimport sys\nsys.path.insert(0, str(Path(__file__).resolve().parents[2]))\n'
    for filename, code in {
        "app.py": 'from runtime.serving.model_runtime import create_app\napp, Model = create_app(Path(__file__).with_name("model.json"))\n',
        "download_model.py": 'from tools.download_models import main as download_main\ndef main(argv=None):\n    download_main([str(Path(__file__).with_name("model.json")), *(argv or [])])\nif __name__ == "__main__":\n    main(sys.argv[1:])\n',
    }.items():
        (target / filename).write_text(bootstrap + code, encoding="utf-8")
    (target / "pyproject.toml").write_text('[project]\nname = "modal-' + service + '"\nversion = "0.1.0"\nrequires-python = ">=3.11"\ndependencies = ["modal>=1.6.3,<2"]\n[tool.uv]\npackage = false\n', encoding="utf-8")
    task_name = directory if re.fullmatch(r"[0-9]{3}(?:-[a-z0-9]+)+", directory) else "006-" + service + "-demo"
    task = {"version": 1, "workflow": workflow_id, "name": task_name, "scene": "001-main", "items": [{"id": "001", "seed": 42, "prompt": "A red paper boat on a lake at sunrise"}]}
    task_path = target / (task_name + ".json")
    task_path.write_text(json.dumps(task, indent=2) + "\n", encoding="utf-8")
    workflow_path.parent.mkdir(parents=True, exist_ok=True)
    workflow_path.write_text(json.dumps({"version": 1, "id": workflow_id,
        "steps": [{"id": "generate", "uses": service + "." + spec["operation"],
                   "inputs": {"prompt": {"ref": "input.prompt"}}}],
        "outputs": {kind: {"ref": "generate." + kind}}}, indent=2) + "\n", encoding="utf-8")
    (target / "README.md").write_text(
        f"# {service}\n\nReview model.json defaults and command against this model's engine requirements.\n"
        "Pin the container image by digest. This generated template has not been GPU validated.\n\n"
        f"From the repository root:\n```powershell\nuv sync\nuv run python run.py plan models/{directory}/{task_path.name}\n"
        f"uv run python models/{directory}/download_model.py --execute\nuv run python -m modal deploy models/{directory}/app.py\n"
        f"# Keep the worker in another terminal\nuv run python run.py worker\nuv run python run.py run models/{directory}/{task_path.name}\n```\n\nSee ../../docs/add-model.md for SDK workflows and recovery boundaries.\n",
        encoding="utf-8")
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("directory", "service", "model", "revision", "image"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--engine", choices=["vllm", "sglang"], required=True)
    parser.add_argument("--kind", choices=["image", "video"], required=True)
    print(scaffold(**vars(parser.parse_args())))
