"""MOSS-specific constraints; defaults live in model.json."""
def validate(params):
    for key in ("guidance_scale", "flow_shift"):
        if type(params[key]) not in (int, float) or not 0 < params[key] < 1000:
            raise ValueError(f"Invalid {key}")
