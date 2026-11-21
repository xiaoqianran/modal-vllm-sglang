"""Prepare base weights and the LoRA adapter on a CPU Modal container."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.serving.model_service import create_download_app, run_download_cli

app, download, check_cache = create_download_app(Path(__file__).with_name("model.json"))


@app.local_entrypoint()
def main(check_only: bool = False, refresh: bool = False, migrate_only: bool = False):
    run_download_cli(download, check_cache, check_only=check_only,
                     refresh=refresh, migrate_only=migrate_only)
