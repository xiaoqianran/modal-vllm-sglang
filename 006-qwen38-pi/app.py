"""Deploy the Qwen3.8 Pi LoRA as a standalone HTTP model service."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.serving.llm_runtime import create_llm_app

app, Model = create_llm_app(Path(__file__).with_name("model.json"))
