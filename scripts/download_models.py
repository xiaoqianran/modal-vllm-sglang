"""Prepare model caches with modal-benchmark's Coordinator and direct Writers."""
from __future__ import annotations

import argparse
import json
import sys
from importlib.metadata import version
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.config.azure_cache import preparation_strategy
from shared.config.model_cache import cache_directory, component_specs
from shared.config.model_spec import validate
from shared.config.pika_overlay import download_sources
from shared.config.storage import VOLUME_NAME
from shared.utils.files import read_json


def plan(spec: dict, workers: int | None = None) -> dict:
    """Build a free, local download plan; never contact HF or Modal."""
    workers = spec.get("download_workers", 2) if workers is None else workers
    if type(workers) is not int or not 1 <= workers <= 5:
        raise ValueError("workers must be an integer between 1 and 5")
    strategy = preparation_strategy(spec)
    components = component_specs(spec)
    sources = (download_sources(spec) if strategy == "sglang_overlay" else [
        {"repo_id": item["model"], "revision": item["revision"],
         "include": ",".join(item.get("allow_patterns", []))}
        for item in components.values()
    ])
    return {"service": spec["service"], "strategy": strategy,
            "volume": VOLUME_NAME, "base": cache_directory(spec) + "/downloads",
            "workers": workers, "sources": sources}


def run(spec: dict, *, workers: int | None = None) -> dict:
    """Download once, then publish exactly one inference-ready cache."""
    from modal_benchmark.hf import create_downloader
    task = plan(spec, workers)
    workers = task["workers"]
    downloader = create_downloader(
        volume_name=VOLUME_NAME, mount_path="/mnt/modal",
        volume_version=None, max_workers=workers, verification_mode="xet",
    )
    volume = downloader.volume
    publisher_image = downloader.image.add_local_python_source("shared")

    if task["strategy"] == "sglang_overlay":
        from shared.serving.model_runtime import build_image
        # The registry image needs the Modal runner dependencies in its Python environment.
        publisher_spec = {
            **spec,
            "uv_packages": (*spec.get("uv_packages", ()), f"modal=={version('modal')}"),
        }
        publisher_image = build_image(publisher_spec, offline=True)

    # Business publication is deliberately separate: generic HF Coordinator
    # cannot know the inference-specific SGLang Overlay/cache contract.
    @downloader.app.function(
        image=publisher_image, cpu=4 if task["strategy"] == "sglang_overlay" else 1,
        memory=8192 if task["strategy"] == "sglang_overlay" else 2048,
        timeout=86400 if task["strategy"] == "sglang_overlay" else 3600,
        volumes={"/mnt/modal": volume}, serialized=True,
    )
    def publish(spec, manifest, base, root, strategy):
        from shared.config.azure_cache import publish_cache
        from shared.config.model_cache import component_specs, verify_cache
        from shared.config.pika_overlay import publish_pika_cache

        volume.reload()
        if strategy == "sglang_overlay":
            published = publish_pika_cache(spec, manifest, base, root, volume)
        else:
            by_repo = {model["repo_id"]: model for model in manifest["models"]}
            models = {name: by_repo[item["model"]]
                      for name, item in component_specs(spec).items()}
            published = publish_cache(spec, models, base, root, volume)
        # Check inference-readiness in the same remote function, not a
        # second container. The cache publishers already commit the marker.
        paths, fingerprint = verify_cache(spec, phase="download_persisted")
        return {**published, "fingerprint": fingerprint,
                "paths": {name: str(path) for name, path in paths.items()}}

    result = downloader.download(task["sources"], base=task["base"], workers=workers)
    if result["status"] != "VERIFIED":
        raise RuntimeError("Model download was not verified")
    with downloader.app.run():
        published = publish.remote(
            spec, result["manifest"], result["base"],
            cache_directory(spec), task["strategy"],
        )
    return {"download": {"status": result["status"],
                          "files": result["files"], "bytes": result["bytes"],
                          "seconds": result["end_to_end_seconds"]},
            "cache": published}


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model_spec", help="Path to model.json")
    parser.add_argument("--workers", type=int, choices=range(1, 6), default=None,
                        help="Override model.json download_workers (default: 2)")
    parser.add_argument("--execute", action="store_true",
                        help="Start paid Modal CPU download and publication")
    args = parser.parse_args(argv)

    spec = validate(read_json(args.model_spec))
    task = plan(spec, args.workers)
    print("PLAN " + json.dumps({**task, "execute": args.execute},
                                ensure_ascii=False), flush=True)
    if args.execute:
        print("RESULT " + json.dumps(run(spec, workers=args.workers),
                                     ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
