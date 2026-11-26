"""Prepare model caches with modal-benchmark's Coordinator and direct Writers."""
from __future__ import annotations

import argparse
import json
import os
import sys
from importlib import import_module
from importlib.metadata import version
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from runtime.config.cache_publication import preparation_strategy
from runtime.config.model_cache import cache_directory, component_specs
from runtime.config.model_spec import validate
from runtime.config.storage import runtime_volume_name, VOLUME_MOUNT, VOLUME_VERSION, VOLUME_SUB_PATH, shared_storage
from runtime.utils.files import read_json


def cache_implementation(spec):
    """Import a model-local cache plugin named in its validated model.json."""
    name = spec.get("cache_implementation")
    if not name:
        raise ValueError("Overlay model must declare a cache_implementation")
    return import_module(name)


def plan(spec: dict, workers: int | str | None = None) -> dict:
    """Build a free, local download plan; never contact HF or Modal."""
    if workers is None:
        raw = os.getenv("MODEL_HF_DOWNLOAD_WORKERS", "").strip()
        workers = ("auto" if raw == "auto" else int(raw)) if raw else spec.get("download_workers", 3)
    if workers != "auto" and (type(workers) is not int or not 1 <= workers <= 8):
        raise ValueError("workers must be auto or an integer between 1 and 8")
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
            "volume": runtime_volume_name(),
            "base": None if shared_storage(spec) else cache_directory(spec) + "/downloads",
            "workers": workers, "sources": sources}


def run(spec: dict, *, workers: int | str | None = None) -> dict:
    """Download once, then publish exactly one inference-ready cache."""
    import modal
    from modal_benchmark.hf import create_downloader
    task = plan(spec, workers)
    workers = task["workers"]
    shared = shared_storage(spec)
    downloader = create_downloader(
        app=modal.App('download-' + spec.get('service_directory', spec['service'])),
        volume_name=task["volume"], mount_path=VOLUME_MOUNT,
        sub_path=VOLUME_SUB_PATH,
        volume_version=VOLUME_VERSION, max_workers=8 if workers == "auto" else workers, verification_mode="xet",
    )
    # Complete the lightweight transfers before constructing the SGLang
    # publisher image. Otherwise Modal may eagerly build the large image
    # while the lightweight download workers are still idle.
    result = downloader.download(
        task["sources"],
        base=(task["base"] if task["base"] is not None else VOLUME_MOUNT + "/shared-hf"),
        workers=workers,
    )
    if result["status"] != "VERIFIED":
        raise RuntimeError("Model download was not verified")
    print("DOWNLOAD_VERIFIED " + json.dumps({
        "service": spec["service"], "files": result["files"],
        "bytes": result["bytes"], "seconds": result["end_to_end_seconds"],
        "worker_policy": result.get("statistics", {}).get("worker_policy"),
    }), flush=True)

    volume = downloader.volume
    publish_mounts = {VOLUME_MOUNT: volume.with_mount_options(sub_path=VOLUME_SUB_PATH)}
    publisher_image = modal.Image.debian_slim(python_version="3.12").add_local_python_source("runtime")

    if task["strategy"] == "sglang_overlay":
        # A lightweight base image with uv-installed SGLang source replaces
        # the full SGLang GPU runtime image for CPU-only materialization.
        publisher_image = cache_implementation(spec).build_publisher_image(
            spec, modal_version=version("modal"),
        )

    # Module-level source is imported by the remote interpreter. Never capture
    # local Modal Volume objects or serialize a closure across Python versions.
    from runtime.serving.remote_workers import publish_cache_entry

    publish = downloader.app.function(
        name="publish", image=publisher_image, cpu=4 if task["strategy"] == "sglang_overlay" else 1,
        memory=8192 if task["strategy"] == "sglang_overlay" else 2048,
        timeout=86400 if task["strategy"] == "sglang_overlay" else 3600,
        volumes=publish_mounts, serialized=False,
    )(publish_cache_entry)

    with downloader.app.run():
        published = publish.remote(
            spec, result["manifest"], result["base"], cache_directory(spec),
            task["strategy"], task["volume"], VOLUME_VERSION,
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
             .add_local_dir(str(PROJECT_ROOT / "runtime"), "/opt/runtime", copy=False, ignore=['**/__pycache__/**', '**/*.pyc', '**/*.pyo']))
    app = modal.App("prepare-" + spec.get("service_directory", spec["service"]))
    from runtime.serving.remote_workers import cache_maintenance_entry

    maintenance = app.function(
        name="maintenance", image=image, cpu=1, memory=2048, timeout=1800,
        serialized=False, max_containers=1, volumes=model_mounts(spec),
    )(cache_maintenance_entry)

    with app.run():
        return maintenance.remote(
            spec, migrate_only, runtime_volume_name(), VOLUME_VERSION
        )


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model_spec", help="Path to model.json")
    parser.add_argument("--workers", type=lambda value: "auto" if value == "auto" else int(value),
                        choices=["auto", *range(1, 9)], default=None,
                        help="Override MODEL_HF_DOWNLOAD_WORKERS and model.json (fallback: 3)")
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
