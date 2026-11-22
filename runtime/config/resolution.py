"""Project canvas presets. Model acceptance remains a separate validation."""
import copy
DEFAULT_RESOLUTION = "1k"

LONG_EDGES = {"256": 256, "512": 512, "768": 768, "1k": 1024, "2k": 2048, "4k": 4096}
RATIOS = {"1:1": (1, 1), "3:4": (3, 4), "4:3": (4, 3), "9:16": (9, 16), "16:9": (16, 9)}
ALIASES = {"pseudo1k": "768", "pseudo-1k": "768", "伪1k": "768", "1024": "1k", "2048": "2k", "4096": "4k"}


def normalize(render):
    if not isinstance(render, dict) or set(render) - {"aspect_ratio", "resolution"}:
        raise ValueError("render accepts only aspect_ratio and resolution")
    ratio, resolution = render.get("aspect_ratio", "1:1"), render.get("resolution", DEFAULT_RESOLUTION)
    if not isinstance(resolution, str) or not isinstance(ratio, str) or ratio not in RATIOS:
        raise ValueError("Use a string resolution and aspect_ratio: " + ", ".join(RATIOS))
    resolution = resolution.lower().strip()
    resolution = ALIASES.get(resolution, resolution)
    if resolution not in LONG_EDGES and resolution not in {"480p", "768p"}:
        raise ValueError("Unknown resolution; use " + ", ".join(LONG_EDGES) + ", 480p or 768p")
    return {"aspect_ratio": ratio, "resolution": resolution}


def image_size(render):
    render = normalize(render)
    if render["resolution"] not in LONG_EDGES:
        raise ValueError("A video-native preset needs a model-specific canvas mapping")
    edge = LONG_EDGES[render["resolution"]]
    x, y = RATIOS[render["aspect_ratio"]]
    width, height = edge * x // max(x, y), edge * y // max(x, y)
    return f"{width}x{height}"


def apply_render_preset(render, steps, specs, parameters):
    """A task-level preset overrides workflow defaults, never model limits."""
    render = normalize(render)
    videos = [step for step in steps.values() if specs[step["uses"]]["kind"] == "video"]
    canvas = None
    for step in videos:
        spec = specs[step["uses"]]
        profiles = spec.get("resolution_profiles", {})
        profile = profiles.get(render["resolution"], {})
        dims = profile.get(render["aspect_ratio"])
        if not dims:
            raise ValueError(f"{spec['service']}: resolution {render['resolution']} at {render['aspect_ratio']} is not a declared native canvas; available presets: {', '.join(profiles) or 'none'}. No automatic upscaling or downgrade.")
        size = f"{dims[0]}x{dims[1]}"
        if canvas is not None and canvas != size:
            raise ValueError("Workflow video models require different native canvases; use separate tasks")
        canvas = size
        supplied = step["supplied_params"]
        target = copy.deepcopy(step["params"]["target"])
        target.update(short_edge=min(dims), aspect_ratio=render["aspect_ratio"])
        supplied["target"] = target
        step["params"] = parameters(step["uses"], supplied)
    canvas = canvas or image_size(render)
    for step in steps.values():
        spec = specs[step["uses"]]
        if spec["kind"] != "image":
            continue
        if "size" not in spec["options"]:
            raise ValueError(f"{spec['service']} does not expose an image size option")
        step["supplied_params"]["size"] = canvas
        step["params"] = parameters(step["uses"], step["supplied_params"])
    return render


def catalog():
    return {tier: {ratio: image_size({"resolution": tier, "aspect_ratio": ratio}) for ratio in RATIOS}
            for tier in LONG_EDGES}


# Keep older workflow clients working while they adopt the explicit name.
apply = apply_render_preset
