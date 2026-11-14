from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.model_service import run_cli

if __name__ == "__main__":
    run_cli(Path(__file__).with_name("model.json"))
