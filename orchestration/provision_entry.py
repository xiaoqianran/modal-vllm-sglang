"""Internal Modal entry point; selected from a discovered model.json."""
import os
from pathlib import Path

from runtime.serving.legacy_cache_preparer import create_download_app
from runtime.serving.model_runtime import create_app

config = Path(os.environ["MODEL_CONFIG"])
if os.environ.get("MODEL_PREPARE") == "1":
    app, download, check_cache = create_download_app(config)

    @app.local_entrypoint()
    def main():
        print(download.remote(repair=True))
else:
    version = os.environ.get("MODEL_DEPLOY_VERSION", "")
    if len(version) != 64 or any(c not in "0123456789abcdef" for c in version):
        raise ValueError("Managed deployment requires a source/cache version hash")
    app, Model = create_app(config)
