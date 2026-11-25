"""Recover a persisted cloud shot without its original local workspace."""
import argparse
import json
from pathlib import Path
import re
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestration import workflow_recovery as recovery
from orchestration.protocol import APP_NAME
from runtime.serving.deployment_probe import versioned_name


def status(volume, manifest, modal_api):
    record = recovery.read_state(volume, manifest)
    owner = recovery.latest_owner(volume, manifest)
    retired = recovery.read_retirement(volume, manifest)
    return {"inputs_retired": retired is not None, "workflow_id": manifest["identity"], "volume": manifest["volume"],
            "payload_hash": manifest["payload_hash"],
            "status": record["status"] if record else "prepared",
            "owner": owner, "owner_terminal": bool(recovery.terminal_evidence(volume, manifest, owner)
                or recovery.owner_terminal(modal_api, owner)) if owner else None,
            "jobs": record["jobs"] if record else manifest["payload"]["initial_jobs"]}


def resume(volume, manifest, modal_api, deployment_version):
    if not isinstance(deployment_version, str) or not re.fullmatch(r"[0-9a-f]{64}", deployment_version):
        raise ValueError("Recovery requires the exact deployed CPU orchestration version")
    record = recovery.read_state(volume, manifest)
    if record and record["status"] in recovery.TERMINAL:
        return {"workflow_id": manifest["identity"], "status": record["status"], "call_id": None}
    if recovery.read_retirement(volume, manifest) is not None:
        raise recovery.ControllerBusy("Retired workflow cannot resume execution")
    owner = recovery.latest_owner(volume, manifest)
    if owner and not recovery.seal_terminal(volume, manifest, modal_api, owner):
        raise recovery.ControllerBusy("Previous controller not proven terminal; no recovery dispatched")
    if record and owner is None:
        raise recovery.ControllerBusy("Checkpoint exists without a durable owner")
    tag = versioned_name("resume_shot", deployment_version)
    info = modal_api.App.lookup(APP_NAME).info()
    if tag not in info.functions:
        raise RuntimeError("Exact recovery entry is not deployed; prepare orchestration first")
    # The remote entry repeats the version and ownership checks. Two simultaneous
    # CLI invocations can dispatch CPU calls, but only one becomes the controller.
    fn = modal_api.Function.from_name(APP_NAME, tag)
    call = fn.spawn(manifest["identity"], manifest["payload_hash"],
                    expected_deployment_version=deployment_version)
    return {"workflow_id": manifest["identity"], "status": "dispatched", "call_id": call.object_id}


def collect(volume, manifest, output):
    from runtime.delivery.local import download_to_local
    record = recovery.read_state(volume, manifest)
    if record is None or record["status"] != "completed":
        raise RuntimeError("Collect requires a completed workflow; inspect or resume first")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    files = {}
    for job in manifest["payload"]["plan"]["jobs"]:
        files[job["key"]] = download_to_local(volume, output, job, record["jobs"][job["key"]], {})
    return {"workflow_id": manifest["identity"], "output": str(output), "files": files}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    for command in ("status", "resume", "collect"):
        sub = subs.add_parser(command)
        sub.add_argument("--volume", required=True, help="Existing Volume V2, not created by recovery")
        sub.add_argument("--id", required=True, help="Cloud workflow identity (64 hex characters)")
        if command == "resume":
            sub.add_argument("--deployment-version", help="Exact CPU version; defaults to local source version")
        if command == "collect":
            sub.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    import modal
    volume = modal.Volume.from_name(args.volume, version=2)
    manifest = recovery.load_plan(volume, args.id, args.volume)
    if args.command == "status":
        result = status(volume, manifest, modal)
    elif args.command == "resume":
        version = args.deployment_version
        if version is None:
            from orchestration.deployment_state import source_version
            version = source_version()
        result = resume(volume, manifest, modal, version)
    else:
        result = collect(volume, manifest, args.out)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    main()
