"""CPU-only: mount an already verified HF snapshot as a shared model, without downloading it."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.config.model_spec import validate
from shared.utils.files import read_json


def run(spec: dict, *, execute: bool = False, replace: bool = False) -> dict:
    from shared.config.storage import shared_storage, shared_volume_name, runtime_volume_name
    if not shared_storage(spec):
        raise ValueError("Set model_storage=shared in model.json")
    plan = {"service": spec["service"], "shared_volume": shared_volume_name(),
            "runtime_volume": runtime_volume_name(), "execute": execute}
    if not execute:
        return plan

    import modal
    from shared.serving.modal_setup import model_mounts

    app = modal.App("attach-shared-hf-" + spec["service"])
    image = (modal.Image.debian_slim(python_version="3.12")
             .add_local_python_source("shared"))
    mounts = model_mounts(spec)
    writable_volume = mounts["/mnt/modal"]

    @app.function(image=image, cpu=1, memory=1024, timeout=300,
                  volumes=mounts, serialized=True)
    def attach(model_spec: dict, allow_replace: bool):
        from shared.config.azure_cache import attach_shared_hf_snapshots
        from shared.config.model_cache import verify_cache

        result = attach_shared_hf_snapshots(
            model_spec, writable_volume, replace=allow_replace)
        paths, fingerprint = verify_cache(model_spec, phase="shared_attached")
        return {**result, "fingerprint": fingerprint,
                "paths": {key: str(path) for key, path in paths.items()}}

    with app.run():
        return {**plan, **attach.remote(spec, replace)}


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
