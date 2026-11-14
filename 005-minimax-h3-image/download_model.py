from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.model_service import create_download_app

app, download, check_cache = create_download_app(Path(__file__).with_name("model.json"))

@app.local_entrypoint()
def main(check_only: bool = False, refresh: bool = False):
    print(check_cache.remote() if check_only else download.remote(refresh=refresh))
