"""Plan, register and monitor inference workflows."""
import argparse
import json

from orchestration import state
from pipeline.s01_build_plan import ROOT, build_plan, new_run
from orchestration.protocol import report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("resolutions", help="List image presets and declared native video canvases; offline")
    preparation = sub.add_parser("prepare", help="Explicitly prepare/deploy models or orchestration; --retry retries failed preparation")
    preparation.add_argument("service", nargs="+")
    preparation.add_argument("--retry", action="store_true")
    for command in ("plan", "run"):
        p = sub.add_parser(command, help="Preview tasks offline" if command == "plan" else
                           "Register tasks and return; the worker executes them")
        p.add_argument("task", nargs="+")
        if command == "run":
            p.add_argument("--output", default="outputs")
            p.add_argument("--dry-run", action="store_true")
    for command in ("resume", "status"):
        p = sub.add_parser(command, help="Register recovery for the worker" if command == "resume" else
                           "Read the last saved progress without cloud access")
        p.add_argument("directory")
    studio = sub.add_parser("studio", help="Start the on-demand local Vue + Python Studio")
    studio.add_argument("--host", default="127.0.0.1")
    studio.add_argument("--port", type=int, default=18765)
    studio.add_argument("--no-browser", action="store_true")
    studio.add_argument("--output", default="outputs")
    check = sub.add_parser("check-deployment", help="Check model deployments; starts GPU, generates no media")
    check.add_argument("service", nargs="+")
    check.add_argument("--dry-run", action="store_true")
    scaling = sub.add_parser("check-scaling", help="Read deployed autoscaler settings and backlog without starting GPU containers")
    scaling.add_argument("service", nargs="+")
    restore = sub.add_parser("restore-scaling", help="Restore an abandoned active-scaling override, if ownership can be proven")
    restore.add_argument("service")
    worker = sub.add_parser("worker", help="Schedule registered SDK scenes using already published deployments")
    worker.add_argument("--output", default="outputs")
    worker.add_argument("--poll", type=float, default=2)
    worker.add_argument("--task", help="Only schedule this registered Task; avoid touching unrelated tasks")
    worker.add_argument("--active-scale", action="store_true",
                        help="Experiment: temporarily request extra capacity for a profitable direct MiniMax Omni batch")
    telemetry = sub.add_parser("telemetry", help="Collect telemetry or report a task timeline")
    telemetry_sub = telemetry.add_subparsers(dest="telemetry_command", required=True)
    collect = telemetry_sub.add_parser("collect")
    collect.add_argument("--output", default="outputs")
    collect.add_argument("--local", action="store_true", help="Index local records without cloud access")
    telemetry_report = telemetry_sub.add_parser("report")
    telemetry_report.add_argument("directory")
    args = parser.parse_args()
    try:
        if args.command == "studio":
            from orchestration.studio_server import serve
            serve(host=args.host, port=args.port, open_browser=not args.no_browser, output=args.output)
            return
        if args.command == "prepare":
            from orchestration.sdk import Backend, model_specs
            from orchestration.provisioning import Provisioner
            specs = model_specs()
            if any(name not in specs and name != "orchestration" for name in args.service):
                raise ValueError("Unknown service; choose orchestration or " + ", ".join(sorted(specs)))
            provisioner = Provisioner(Backend())
            try:
                keys = [provisioner.start(specs.get(name), retry=args.retry) for name in args.service]
                for key in keys:
                    future = provisioner.futures.get(key)
                    if future:
                        future.result()
                results = [provisioner.registry.get(key, {}) for key in keys]
                print(json.dumps(results, ensure_ascii=False, indent=2))
                if any(result.get("status") != "ready" for result in results):
                    raise SystemExit(1)
            finally:
                provisioner.close()
            return
        if args.command == "resolutions":
            from runtime.config.resolution import catalog, ALIASES, DEFAULT_RESOLUTION
            from orchestration.sdk import model_specs
            print(json.dumps({"default_image_resolution": DEFAULT_RESOLUTION, "image_presets": catalog(), "aliases": ALIASES,
                              "native_video_profiles": {name: spec.get("resolution_profiles", {})
                                                        for name, spec in model_specs().items() if spec["kind"] == "video"}},
                             ensure_ascii=False, indent=2))
            return
        if args.command == "telemetry":
            from runtime.telemetry.recorder import flush_local
            if args.telemetry_command == "collect":
                from runtime.telemetry.collector import collect as collect_records
                output = state.runtime_root(state.local_path(ROOT, args.output)) / "logs"
                flush_local(output)
                volume = None
                if not args.local:
                    from runtime.serving.modal_setup import model_volume
                    volume = model_volume()
                result = collect_records(output, volume)
            else:
                from orchestration.sdk import snapshot
                from runtime.telemetry.report import task_report
                directory = state.resolve_task(ROOT, args.directory)
                plan, _ = snapshot(directory)
                output = state.telemetry_directory(directory)
                flush_local(output)
                result = task_report(output, f"{plan['project']}/{plan['scene']}")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if args.command in {"plan", "run"}:
            plans = [build_plan(path) for path in args.task]
            if args.command == "plan" or args.dry_run:
                for plan in plans:
                    print(json.dumps({"name": plan["name"], "render": plan.get("render"), "transport": "sdk", "calls": len(plan["jobs"]),
                                      "services": list(plan["services"]), "delivery": plan.get("delivery", {"provider": "local"}), "jobs": plan["jobs"]}, ensure_ascii=False, indent=2))
                return
            # Cloud-owned credentials: do not require or transmit a PC2 HF token.
            # Verify deployed CPU archiver and its Modal Secret before GPU work.
            hf_buckets = {plan["delivery"]["bucket"] for plan in plans
                          if plan.get("delivery", {}).get("provider") in {"huggingface", "both"}}
            if hf_buckets:
                from runtime.delivery import v3
                from orchestration.delivery_preflight import check_delivery
                checks = {(plan["delivery"]["bucket"], plan["delivery_app"], plan["delivery_volume"], plan["volume"])
                          for plan in plans if plan.get("delivery_protocol") == v3.PROTOCOL}
                for check in sorted(checks):
                    check_delivery(*check)
            directories = [new_run(plan, output=args.output) for plan in plans]
            from orchestration.sdk import request_resume
            for directory in directories:
                request_resume(directory)
                print(f"Registered: {directory}", flush=True)
            print("Registration complete. Keep `python run.py worker` running to execute jobs.", flush=True)
            return
        elif args.command == "check-scaling":
            from orchestration.sdk import model_specs
            from orchestration.scaling import inspect_scaling
            specs = model_specs()
            if any(service not in specs for service in args.service):
                raise ValueError("Unknown service; choose from " + ", ".join(sorted(specs)))
            results = [inspect_scaling(specs[service]) for service in args.service]
            print(json.dumps(results, ensure_ascii=False, indent=2))
            if any(not result["matches"] for result in results):
                raise SystemExit(1)
            return
        elif args.command == "restore-scaling":
            from orchestration.sdk import Backend, model_specs
            from orchestration.protocol import PROVISION_REGISTRY
            from orchestration.scaling import restore_capacity
            specs = model_specs()
            if args.service not in specs:
                raise ValueError("Unknown service; choose from " + ", ".join(sorted(specs)))
            backend = Backend()
            registry = backend.modal.Dict.from_name(PROVISION_REGISTRY)
            print(json.dumps(restore_capacity(specs[args.service], backend.modal, registry),
                             ensure_ascii=False, indent=2))
            return
        elif args.command == "check-deployment":
            from orchestration.sdk import check_deployment, model_specs
            specs = model_specs()
            if any(service not in specs for service in args.service):
                raise ValueError("Unknown service; choose from " + ", ".join(sorted(specs)))
            for service in args.service:
                print(json.dumps(check_deployment(specs[service], dry_run=args.dry_run), ensure_ascii=False))
            return
        elif args.command == "worker":
            from pipeline.s02_submit_shots import worker as run_worker
            from orchestration.monitor import LogStream
            from contextlib import redirect_stdout, redirect_stderr
            import sys
            output = state.local_path(ROOT, args.output)
            logs = state.runtime_root(output) / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            stream = LogStream(sys.stdout, logs / "worker.log")
            try:
                with redirect_stdout(stream), redirect_stderr(stream):
                    options = {"task_name": args.task} if args.task else {}
                    if args.active_scale:
                        if not args.task:
                            raise ValueError("--active-scale requires --task for the bounded experiment")
                        options["active_scale"] = True
                    run_worker(output, poll=args.poll, **options)
            finally:
                stream.close()
            return
        else:
            directory = state.resolve_task(ROOT, args.directory)
        if args.command == "status":
            from orchestration.sdk import snapshot
            plan, current = snapshot(directory)
            result = report(plan, current["jobs"])
        else:
            from orchestration.sdk import request_resume
            request_resume(directory)
            print("Resume registered. The worker will query original calls and retry cloud delivery.", flush=True)
            return
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("download_errors") or result.get("archive_errors") or result.get("delivery_pending") or any(status != "completed" and count for status, count in result["counts"].items()):
            raise SystemExit(1)
    except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
        parser.exit(2, f"{type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    main()
