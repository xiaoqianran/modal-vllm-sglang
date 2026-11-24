"""Verify a prepared shared Volume V2 model from a fresh CPU container, no GPU."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runtime.config.model_spec import validate
from runtime.config.storage import shared_storage
from runtime.serving.modal_setup import model_mounts
from runtime.serving.remote_workers import check_shared_cache_entry
from runtime.utils.files import read_json


def run(spec: dict) -> dict:
    if not shared_storage(spec):
        raise ValueError("This check requires model_storage=shared")
    import modal

    app = modal.App("check-shared-model-" + spec["service"])
    image = (
        modal.Image.debian_slim(python_version="3.12")
        .add_local_python_source("runtime")
    )
    check = app.function(
        name="check", image=image, cpu=1, memory=1024, timeout=300,
        volumes=model_mounts(spec), serialized=False,
    )(check_shared_cache_entry)
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
