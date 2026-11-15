"""MOSS-specific constraints; defaults live in model.json."""
def validate(params):
    width, height = map(int, params["size"].split("x"))
    if width <= 0 or height <= 0 or width % 16 or height % 16:
        raise ValueError("MOSS size must use positive multiples of 16")
    for key in ("guidance_scale", "flow_shift"):
        if type(params[key]) not in (int, float) or not 0 < params[key] < 1000:
            raise ValueError(f"Invalid {key}")
