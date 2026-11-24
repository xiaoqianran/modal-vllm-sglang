"""CPU-only: mount an already verified HF snapshot as a shared model, without downloading it."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runtime.config.model_spec import validate
from runtime.config.storage import VOLUME_SUB_PATH, VOLUME_VERSION, runtime_volume_name
from runtime.serving.modal_setup import model_mounts
from runtime.serving.remote_workers import attach_shared_hf_entry
from runtime.utils.files import read_json


def run(spec: dict, *, execute: bool = False, replace: bool = False) -> dict:
    from runtime.config.storage import shared_storage

    if not shared_storage(spec):
        raise ValueError("Set model_storage=shared in model.json")
    plan = {"service": spec["service"], "volume": runtime_volume_name(), "execute": execute}
    if not execute:
        return plan

    import modal

    app = modal.App("attach-shared-hf-" + spec["service"])
    image = (
        modal.Image.debian_slim(python_version="3.12")
        .add_local_python_source("runtime")
    )
    attach = app.function(
        name="attach", image=image, cpu=1, memory=1024, timeout=300,
        volumes=model_mounts(spec), serialized=False,
    )(attach_shared_hf_entry)
    with app.run():
        result = attach.remote(
            spec, replace, runtime_volume_name(), VOLUME_VERSION, VOLUME_SUB_PATH
        )
    return {**plan, **result}


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model_spec", type=Path)
    parser.add_argument("--execute", action="store_true",
                        help="Publish model-ready.json into the writable service Volume")
    parser.add_argument("--replace", action="store_true",
                        help="Explicitly replace an existing model-ready.json")
    args = parser.parse_args(argv)
    spec = validate(read_json(args.model_spec))
    print(json.dumps(run(spec, execute=args.execute, replace=args.replace),
                     ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
