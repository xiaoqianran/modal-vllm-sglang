"""Pinned image and text-encoder snapshots; GPU startup never downloads."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.cache import file_inventory

MODEL = "MiniMaxAI/MiniMax-H3-Image"
MODEL_REVISION = "5f90473023975e6271b0ed1dd84c4e88e4bbc6d3"
ENCODER = "MiniMaxAI/MiniMax-H3"
ENCODER_REVISION = "0919fb684102ef115126612f3fbb84d1027e7317"
HF_HOME = "/mnt/modal/003-minimax-h3-image"
SPECS = {
    "image": (MODEL, MODEL_REVISION, ["model_index.json", "pdd_config.json",
              "pdd_fused_heads.safetensors", "transformer/*", "vae/*", "tokenizer/*", "processor/*"]),
    "encoder": (ENCODER, ENCODER_REVISION, ["text_encoder/*"]),
}


def get_ready_model():
    root = Path(os.environ.get("HF_HOME", HF_HOME))
    marker = root / "model-ready.json"
    if not marker.is_file():
        raise RuntimeError("Run: uv run modal run download_model.py")
    record = json.loads(marker.read_text(encoding="utf-8"))
    if record.get("manifest_version") != 1:
        raise RuntimeError("Unsupported MiniMax cache manifest")
    paths = {}
    for name, (repo, revision, _) in SPECS.items():
        entry = record.get(name, {})
        relative = f"models--{repo.replace('/', '--')}/snapshots/{revision}"
        if (entry.get("repo_id"), entry.get("revision"), entry.get("snapshot_relative")) != (repo, revision, relative):
            raise RuntimeError(f"MiniMax {name} cache identity mismatch")
        path = root / "hub" / relative
        inventory = file_inventory(path)
        if inventory != entry.get("inventory"):
            raise RuntimeError(f"MiniMax {name} cache changed or incomplete")
        components = ("transformer", "vae") if name == "image" else ("text_encoder",)
        for component in components:
            if not any(p.startswith(component + "/") and p.endswith(".safetensors") for p in inventory):
                raise RuntimeError(f"Missing {component} weights")
        if name == "image" and not {"model_index.json", "pdd_config.json", "pdd_fused_heads.safetensors"} <= inventory.keys():
            raise RuntimeError("Missing MiniMax image configuration or PDD heads")
        paths[name] = path
    return str(paths["image"]), str(paths["encoder"] / "text_encoder")
