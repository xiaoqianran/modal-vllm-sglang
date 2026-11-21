"""Prepare model caches using provider-neutral Modal downloads and SGLang overlays."""
import argparse
from contextlib import nullcontext
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    import modal
    from modal_benchmark.hf import create_downloader
    from shared.config.model_spec import validate
    from shared.config.model_cache import component_specs, cache_directory
    from shared.config.azure_cache import publish_cache, preparation_strategy
    from shared.config.pika_overlay import download_sources, publish_pika_cache
    from shared.config.storage import VOLUME_NAME
    from shared.tasks.batch import intent_digest
    from shared.utils.files import read_json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model_spec", help="Path to model.json")
    parser.add_argument("--workers", default="2", choices=["auto", *map(str, range(1, 6))])
    parser.add_argument("--max-workers", type=int, default=2, choices=range(1, 6))
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.workers != "auto" and int(args.workers) > args.max_workers:
        parser.error("--workers exceeds --max-workers")

    spec = validate(read_json(args.model_spec))
    strategy = preparation_strategy(spec)
    if strategy == "sglang_overlay":
        sources = download_sources(spec)
    else:
        sources = [
            {"repo_id": config["model"], "revision": config["revision"],
             "include": ",".join(config.get("allow_patterns", []))}
            for config in component_specs(spec).values()
        ]

    downloader = create_downloader(volume_name=VOLUME_NAME, mount_path="/mnt/modal",
                                   volume_version=None, max_workers=args.max_workers)
    volume = downloader.volume
    image = downloader.image.add_local_python_source("shared")

    @downloader.app.function(image=image,
        region=downloader.config.region, cpu=0.5, memory=1024, timeout=3600,
        volumes={"/mnt/modal": volume}, serialized=True)
    def publish(spec, models, base, root):
        volume.reload()
        return publish_cache(spec, models, base, root, volume)

    @downloader.app.function(image=image,
        region=downloader.config.region, cpu=0.5, memory=1024, timeout=3600,
        volumes={"/mnt/modal": volume}, serialized=True)
    def check_inference_cache(spec):
        from shared.config.model_cache import verify_cache
        volume.reload()
        paths, fingerprint = verify_cache(spec, phase="download_persisted")
        return {"ready": True, "fingerprint": fingerprint,
                "paths": {name: str(path) for name, path in paths.items()}}

    if strategy == "sglang_overlay" and args.execute:
        from shared.serving.model_runtime import build_image
        sglang_image = build_image(spec, offline=True)

        @downloader.app.function(image=sglang_image,
            region=downloader.config.region, cpu=4, memory=8192, timeout=86400,
            volumes={"/mnt/modal": volume}, serialized=True)
        def publish_overlay(spec, manifest, base, root):
            # All writer commits must be visible before materialization.
            volume.reload()
            return publish_pika_cache(spec, manifest, base, root, volume)

    # Modal Rich progress output can crash on Windows GBK when stdout is piped.
    output = modal.enable_output() if sys.stdout.isatty() else nullcontext()
    with output, downloader.app.run():
        manifest = downloader.discover_many(sources)
        if strategy == "sglang_overlay":
            models = None
        else:
            models = dict(zip(component_specs(spec), manifest["models"]))
        root = cache_directory(spec)
        # Preserve existing Volume layout for completed or partially downloaded tasks.
        base = root + "/azure/" + intent_digest(manifest)
        print(json.dumps({"service": spec["service"], "strategy": strategy,
            "volume": VOLUME_NAME, "base": base, "provider_policy": "any",
            "workers": args.workers, "max_workers": args.max_workers,
            "files": sum(len(m["files"]) for m in manifest["models"]),
            "bytes": sum(f["size"] for m in manifest["models"] for f in m["files"].values())}))
        if args.execute:
            downloader.run_manifest(manifest, base, workers=args.workers,
                                    max_workers=args.max_workers)
            if strategy == "sglang_overlay":
                print(json.dumps(publish_overlay.remote(spec, manifest, base, root)))
            else:
                print(json.dumps(publish.remote(spec, models, base, root)))
            print(json.dumps(check_inference_cache.remote(spec)))


if __name__ == "__main__":
    main()
