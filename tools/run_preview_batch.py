"""Submit H3 generation/edit pairs to the deployed model; never relay image bytes."""
import argparse
import json
from pathlib import Path

from orchestration.sdk import Backend, model_specs
from orchestration.deployment_state import model_source_version
from runtime.archive.config import DEFAULT_ASSET_BUCKET
from runtime.delivery import outbox, v3
from runtime.serving.preview_delivery import preview_intents
from runtime.tasks.task_identity import intent_digest
from runtime.utils.files import atomic_json, read_json
from runtime.utils.outputs import validate_path_component


def submit(project, pairs, output, *, archive=True):
    validate_path_component(project)
    output = Path(output)
    backend = Backend()
    modal = backend.modal
    spec = model_specs()["minimax"]
    if not spec.get("enable_image_edits"):
        raise ValueError("H3 edits are disabled")
    version = intent_digest([model_source_version(spec), spec, backend.fingerprint(spec)])
    from runtime.serving.deployment_probe import probe_name
    if modal.Function.from_name(spec["app_name"], probe_name(version)).remote() != version:
        raise RuntimeError("Deployment version mismatch")
    output.mkdir(parents=True, exist_ok=False)
    atomic_json(output / "plan.json", pairs)
    intents = preview_intents(project, pairs) if archive else {}
    if archive:
        store = v3.ledger(modal)
        info = modal.Function.from_name(v3.app_name(), "check_bucket").remote(DEFAULT_ASSET_BUCKET, v3.PROTOCOL)
        if (not info.get("ready") or info.get("protocol") != v3.PROTOCOL
                or info.get("build") != v3.build_id()
                or info.get("volume_id") != backend.volume.hydrate().object_id
                or info.get("delivery_volume_id") != store.volume.hydrate().object_id):
            raise RuntimeError("V3 delivery/storage mismatch")
        atomic_json(output / "delivery-intents.json", list(intents.values()))
        for intent in intents.values():
            v3.register(store, intent)
    model = modal.Cls.from_name(spec["app_name"], spec.get("modal_class", "Model"))()
    call = model.preview_batch.spawn(project, pairs, archive, expected_deployment_version=version)
    atomic_json(output / "call.json", {"call_id": call.object_id, "project": project, "version": version})
    if archive:
        for intent in intents.values():
            outbox.immutable(store, outbox.path(intent["id"], "generation-call.json"), {"call_id": call.object_id})
    report = call.get()
    atomic_json(output / "results.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project")
    parser.add_argument("plan", type=Path, help="JSON list of generation/edit pairs")
    parser.add_argument("output", type=Path, help="New metadata-only output directory")
    parser.add_argument("--no-archive", action="store_true")
    args = parser.parse_args()
    report = submit(args.project, read_json(args.plan), args.output, archive=not args.no_archive)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
