"""Prepare model caches with modal-benchmark's Coordinator and direct Writers."""
from __future__ import annotations

import argparse
import json
import sys
from importlib import import_module
from importlib.metadata import version
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from runtime.config.cache_publication import preparation_strategy
from runtime.config.model_cache import cache_directory, component_specs
from runtime.config.model_spec import validate
from runtime.config.storage import runtime_volume_name, SHARED_MOUNT, shared_storage, shared_volume_name
from runtime.utils.files import read_json


def cache_implementation(spec):
    """Import a model-local cache plugin named in its validated model.json."""
    name = spec.get("cache_implementation")
    if not name:
        raise ValueError("Overlay model must declare a cache_implementation")
    return import_module(name)


def plan(spec: dict, workers: int | None = None) -> dict:
    """Build a free, local download plan; never contact HF or Modal."""
    workers = spec.get("download_workers", 2) if workers is None else workers
    if type(workers) is not int or not 1 <= workers <= 5:
        raise ValueError("workers must be an integer between 1 and 5")
    strategy = preparation_strategy(spec)
    if shared_storage(spec) and strategy != "hf":
        raise ValueError("Shared storage supports HF snapshots, not SGLang Overlay")
    components = component_specs(spec)
    if strategy == "sglang_overlay":
        sources = cache_implementation(spec).download_sources(spec)
    else:
        sources = [
            {"repo_id": item["model"], "revision": item["revision"],
             "include": ",".join(item.get("allow_patterns", []))}
            for item in components.values()
        ]
    return {"service": spec["service"], "strategy": strategy,
            "volume": shared_volume_name() if shared_storage(spec) else runtime_volume_name(),
            "base": None if shared_storage(spec) else cache_directory(spec) + "/downloads",
            "workers": workers, "sources": sources}


def run(spec: dict, *, workers: int | None = None) -> dict:
    """Download once, then publish exactly one inference-ready cache."""
    from modal_benchmark.hf import create_downloader
    task = plan(spec, workers)
    workers = task["workers"]
    shared = shared_storage(spec)
    downloader = create_downloader(
        volume_name=task["volume"], mount_path=SHARED_MOUNT if shared else "/mnt/modal",
        volume_version=2, max_workers=workers, verification_mode="xet",
    )
    # Complete the lightweight transfers before constructing the SGLang
    # publisher image. Otherwise Modal may eagerly build the large image
    # while the lightweight download workers are still idle.
    result = downloader.download(task["sources"], base=task["base"], workers=workers)
    if result["status"] != "VERIFIED":
        raise RuntimeError("Model download was not verified")
    print("DOWNLOAD_VERIFIED " + json.dumps({
        "service": spec["service"], "files": result["files"],
        "bytes": result["bytes"], "seconds": result["end_to_end_seconds"],
    }), flush=True)

    volume = downloader.volume
    if shared:
        from runtime.serving.modal_setup import model_volume
        writable_volume = model_volume()
        publish_mounts = {
            "/mnt/modal": writable_volume,
            SHARED_MOUNT: volume.with_mount_options(read_only=True),
        }
    else:
        writable_volume = volume
        publish_mounts = {"/mnt/modal": volume}
    publisher_image = downloader.image.add_local_python_source("runtime")

    if task["strategy"] == "sglang_overlay":
        # A lightweight base image with uv-installed SGLang source replaces
        # the full SGLang GPU runtime image for CPU-only materialization.
        publisher_image = cache_implementation(spec).build_publisher_image(
            spec, modal_version=version("modal"),
        )

    # Business publication is deliberately separate: generic HF Coordinator
    # cannot know the inference-specific SGLang Overlay/cache contract.
    @downloader.app.function(
        image=publisher_image, cpu=4 if task["strategy"] == "sglang_overlay" else 1,
        memory=8192 if task["strategy"] == "sglang_overlay" else 2048,
        timeout=86400 if task["strategy"] == "sglang_overlay" else 3600,
        volumes=publish_mounts, serialized=True,
    )
    def publish(spec, manifest, base, root, strategy):
        from runtime.config.cache_publication import publish_cache
        from runtime.config.model_cache import component_specs, verify_cache

        volume.reload()
        if strategy == "sglang_overlay":
            # The publisher image mounts the model-owned implementation package.
            from importlib import import_module
            implementation = import_module(spec["cache_implementation"])
            published = implementation.publish_cache(spec, manifest, base, root, writable_volume)
        else:
            by_repo = {model["repo_id"]: model for model in manifest["models"]}
            models = {name: by_repo[item["model"]]
                      for name, item in component_specs(spec).items()}
            published = publish_cache(spec, models, base, root, writable_volume)
        # Check inference-readiness in the same remote function, not a
        # second container. The cache publishers already commit the marker.
        paths, fingerprint = verify_cache(spec, phase="download_persisted")
        return {**published, "fingerprint": fingerprint,
                "paths": {name: str(path) for name, path in paths.items()}}

    with downloader.app.run():
        published = publish.remote(
            spec, result["manifest"], result["base"],
            cache_directory(spec), task["strategy"],
        )
    return {"download": {"status": result["status"],
                          "files": result["files"], "bytes": result["bytes"],
                          "seconds": result["end_to_end_seconds"]},
            "cache": published}


def run_cache_maintenance(spec, *, migrate_only=False):
    """Read/migrate existing metadata on a slim CPU worker, never on a GPU."""
    import modal
    from runtime.serving.modal_setup import model_mounts

    image = (modal.Image.debian_slim(python_version="3.12")
             .env({"PYTHONPATH": "/opt"})
             .add_local_dir(str(PROJECT_ROOT / "runtime"), "/opt/runtime", copy=False))
    app = modal.App("model-cache-maintenance-" + spec["service"])

    @app.function(image=image, cpu=1, memory=2048, timeout=1800,
                  serialized=True, max_containers=1, volumes=model_mounts(spec))
    def maintenance(config, migrate):
        from runtime.serving.modal_setup import model_volume
        from runtime.config.model_cache import inspect_or_migrate_cache
        volume = model_volume()
        volume.reload()
        return inspect_or_migrate_cache(config, volume, migrate_only=migrate)

    with app.run():
        return maintenance.remote(spec, migrate_only)


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model_spec", help="Path to model.json")
    parser.add_argument("--workers", type=int, choices=range(1, 6), default=None,
                        help="Override model.json download_workers (default: 2)")
    parser.add_argument("--execute", action="store_true",
                        help="Explicitly allow paid Modal CPU execution (without this: plan only)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-only", action="store_true",
                      help="Verify existing remote cache without any downloads")
    mode.add_argument("--migrate-only", action="store_true",
                      help="Convert an existing legacy manifest; never download")
    parser.add_argument("--allow-archived-download", "--allow-legacy-qwen",
                        dest="allow_archived_download", action="store_true",
                        help="Explicitly authorize downloading a model with download_policy=archived")
    args = parser.parse_args(argv)

    spec = validate(read_json(args.model_spec))
    task = plan(spec, args.workers)
    action = "check" if args.check_only else "migrate" if args.migrate_only else "download"
    print("PLAN " + json.dumps({**task, "action": action, "execute": args.execute},
                                ensure_ascii=False), flush=True)
    if args.execute:
        if (spec.get("download_policy", "active") == "archived" and action == "download"
                and not args.allow_archived_download):
            parser.error("This model's download_policy=archived; "
                         "pass --allow-archived-download to authorize its historical weights")
        result = (run_cache_maintenance(spec, migrate_only=args.migrate_only)
                  if args.check_only or args.migrate_only
                  else run(spec, workers=args.workers))
        print("RESULT " + json.dumps(result,
                                     ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
