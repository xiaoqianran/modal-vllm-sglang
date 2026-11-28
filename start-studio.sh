#!/usr/bin/env sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ -x ".venv/bin/python" ]; then
  exec .venv/bin/python run.py studio "$@"
fi
if command -v uv >/dev/null 2>&1; then
  exec uv run python run.py studio "$@"
fi
if command -v python3.11 >/dev/null 2>&1; then
  exec python3.11 run.py studio "$@"
fi
if command -v python3 >/dev/null 2>&1; then
  exec python3 run.py studio "$@"
fi
echo "Python 3.11+ or uv is required." >&2
exit 1
