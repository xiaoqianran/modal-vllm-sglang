"""Stage an edit-ready H3 app under a new name, never replacing production."""
import json
from pathlib import Path
import sys
from tempfile import mkdtemp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from runtime.serving.model_runtime import create_app

spec = json.loads((ROOT / "models/005-minimax-h3-image/model.json").read_text())
spec.update(app_name="image-studio-h3-preview", endpoint_label="image-studio-h3-preview",
            enable_image_edits=True)
config = Path(mkdtemp(prefix="image-studio-h3-")) / "model.json"
config.write_text(json.dumps(spec), encoding="utf-8")
app, Model = create_app(config)
