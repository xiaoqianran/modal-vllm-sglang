"""Scaffold a standard Modal image/video service without editing shared code."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from shared.outputs import component


def scaffold(directory, service, engine, kind, model, revision, image):
    component(directory)
    component(service)
    if engine not in {"vllm", "sglang"} or kind not in {"image", "video"}:
        raise ValueError("Expected vllm/sglang and image/video")
    target = ROOT / directory
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
    from shared.model_spec import validate, discover
    validate(spec)
    if any(existing["service"] == service for existing in discover(ROOT).values()):
        raise ValueError(f"Service already exists: {service}")
    target.mkdir()
    (target / "model.json").write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    bootstrap = 'from pathlib import Path\nimport sys\nsys.path.insert(0, str(Path(__file__).resolve().parent.parent))\n'
    for filename, code in {
        "app.py": 'from shared.model_service import create_app\napp, Model = create_app(Path(__file__).with_name("model.json"))\n',
        "download_model.py": 'from shared.model_service import create_download_app\napp, download, check_cache = create_download_app(Path(__file__).with_name("model.json"))\n@app.local_entrypoint()\ndef main(check_only: bool = False, refresh: bool = False):\n    print(check_cache.remote() if check_only else download.remote(refresh=refresh))\n',
        "run.py": 'from shared.model_service import run_cli\nif __name__ == "__main__":\n    run_cli(Path(__file__).with_name("model.json"))\n',
    }.items():
        (target / filename).write_text(bootstrap + code, encoding="utf-8")
    (target / "pyproject.toml").write_text('[project]\nname = "modal-' + service + '"\nversion = "0.1.0"\nrequires-python = ">=3.11"\ndependencies = ["modal>=1.6.3,<2"]\n[tool.uv]\npackage = false\n', encoding="utf-8")
    task = {"version": 1, "workflow": service + "-" + spec["operation"], "name": "demo", "scene": "001-main", "items": [{"id": "001", "seed": 42, "prompt": "A red paper boat on a lake at sunrise"}]}
    (target / "task.example.json").write_text(json.dumps(task, indent=2) + "\n", encoding="utf-8")
    (target / "README.md").write_text(
        f"# {service}\n\nReview model.json defaults and command against this model's engine requirements.\n"
        "Pin the container image by digest. This generated template has not been GPU validated.\n\n"
        f"From the repository root:\n```powershell\nuv sync --project {directory}\nuv run python {directory}/run.py --task {directory}/task.example.json --dry-run\n"
        f"uv run python -m modal run {directory}/download_model.py\nuv run python -m modal deploy {directory}/app.py\n"
        f"uv run python {directory}/run.py --task {directory}/task.example.json\n```\n\nSee ../docs/add-model.md for HTTP workflows and recovery boundaries.\n",
        encoding="utf-8")
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("directory", "service", "model", "revision", "image"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--engine", choices=["vllm", "sglang"], required=True)
    parser.add_argument("--kind", choices=["image", "video"], required=True)
    print(scaffold(**vars(parser.parse_args())))
