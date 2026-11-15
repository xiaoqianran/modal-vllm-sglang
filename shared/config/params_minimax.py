"""MiniMax image size constraints; defaults live in model.json."""
def validate(params):
    width, height = map(int, params["size"].split("x"))
    if width % 32 or height % 32 or not 4194304 <= width * height <= 20971520:
        raise ValueError("MiniMax size must be 32-pixel aligned, between 4 and 20 MP")
