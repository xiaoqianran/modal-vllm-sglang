from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from runtime.serving.model_runtime import create_app

app, Model = create_app(Path(__file__).with_name("model.json"))
