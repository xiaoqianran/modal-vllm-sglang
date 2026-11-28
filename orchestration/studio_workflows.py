"""Offline discovery of Atelier-compatible, single-image workflows."""
from pathlib import Path
from runtime.config.resolution import catalog
from orchestration.model_capabilities import MODEL_SPECS, parameters
from runtime.utils.files import read_json


def image_workflows(root):
    """Expose actual workflow bindings and validated canvases, without cloud calls."""
    models = {}
    for path in sorted((Path(root) / "workflows").glob("*.json")):
        workflow = read_json(path)
        steps = workflow.get("steps", [])
        if len(steps) != 1 or workflow.get("id") != path.stem:
            continue
        step = steps[0]
        uses = step.get("uses", "")
        spec = MODEL_SPECS.get(uses)
        if not spec or spec["kind"] != "image":
            continue
        mode = spec["operation"]
        if mode not in {"t2i", "i2i"}:
            continue
        expected = {"prompt", "image"} if mode == "i2i" else {"prompt"}
        bindings = step.get("inputs", {})
        if not expected <= bindings.keys() or bindings.keys() - expected - {"seed"}:
            continue
        if any(value != {"ref": "input." + key} for key, value in bindings.items()):
            continue
        if not any(value == {"ref": step["id"] + ".image"} for value in workflow.get("outputs", {}).values()):
            continue
        canvases = {}
        for tier, ratios in catalog().items():
            for ratio, size in ratios.items():
                try:
                    result = parameters(uses, {**step.get("params", {}), "size": size})
                except ValueError:
                    continue
                # Do not advertise a silently normalized or downgraded canvas.
                if result.get("size") == size:
                    canvases.setdefault(tier, {})[ratio] = list(map(int, size.split("x")))
        if not canvases:
            continue
        service = spec["service"]
        entry = models.setdefault(service, {"id": service, "label": spec["model"].rsplit("/", 1)[-1] + " · " + {"minimax": "SGLang", "minimax-omni": "vLLM Omni"}.get(service, service),
                                            "workflows": {}, "canvases": {}, "seeds": {}})
        # Prefer the canonical service-operation workflow over alternative templates.
        if mode in entry["workflows"] and path.stem != service + "-" + mode:
            continue
        entry["workflows"][mode] = path.stem
        entry["canvases"][mode] = canvases
        entry["seeds"][mode] = True
    return list(models.values())
