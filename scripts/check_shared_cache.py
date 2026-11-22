"""Verify a prepared shared Volume V2 model from a fresh CPU container, no GPU."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.config.model_spec import validate
from shared.config.storage import shared_storage
from shared.utils.files import read_json


def run(spec: dict) -> dict:
    if not shared_storage(spec):
        raise ValueError("This check requires model_storage=shared")
    import modal
    from shared.serving.modal_setup import model_mounts

    app = modal.App("check-shared-model-" + spec["service"])

    @app.function(
        image=modal.Image.debian_slim(python_version="3.12").add_local_python_source("shared"),
        cpu=1, memory=1024, timeout=300, volumes=model_mounts(spec), serialized=True,
    )
    def check(model_spec: dict):
        from shared.config.model_cache import verify_cache, command_argv
        paths, fingerprint = verify_cache(model_spec, phase="fresh_cpu_start")
        return {"ready": True, "fingerprint": fingerprint,
                "paths": {name: str(value) for name, value in paths.items()},
                "command": command_argv(model_spec, paths)}

    with app.run():
        return check.remote(spec)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model_spec", type=Path)
    args = parser.parse_args(argv)
    spec = validate(read_json(args.model_spec))
    print(json.dumps(run(spec), ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
