"""Deploy Pi LoRA using the GitHub-hosted Qwen3.8 SGLang backend.

No sibling-directory dependency: a reusable Git checkout is managed separately.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

REPOSITORY = "https://github.com/xiaoqianran/Qwen3.8-27B-RTX-6000-PRO-SGLang-DSpark.git"
DEFAULT_REF = "main"
MODEL_NAME = "qwen3.8-27b:qwen3.8-27B-pi"
DEFAULT_CACHE = Path.home() / ".cache" / "qwen38-pi" / "backend"


def run_git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=cwd, text=True, encoding="utf-8"
    ).strip()


def resolve_backend(
    *,
    backend_path: Path | None = None,
    cache_dir: Path = DEFAULT_CACHE,
    ref: str = DEFAULT_REF,
    update: bool = False,
    dry_run: bool = False,
) -> Path:
    """Locate an explicit checkout or manage a cached GitHub checkout."""
    if backend_path is not None:
        if update:
            raise ValueError("--update-backend cannot modify --backend-path")
        backend = backend_path.expanduser().resolve()
        if not (backend / "deploy" / "modal_app.py").is_file():
            raise FileNotFoundError(f"Invalid backend checkout: {backend}")
        return backend

    backend = cache_dir.expanduser().resolve()
    if not backend.exists():
        if dry_run:
            return backend
        backend.parent.mkdir(parents=True, exist_ok=True)
        run_git("clone", "--depth=1", "--filter=blob:none", "--branch", ref, REPOSITORY, str(backend))
    elif not (backend / ".git").exists():
        raise RuntimeError(f"Cache path exists but is not a Git checkout: {backend}")

    if update and not dry_run:
        if run_git("status", "--porcelain", cwd=backend):
            raise RuntimeError("Cached checkout contains local changes; refusing update")
        run_git("fetch", "--depth=1", "--filter=blob:none", "origin", ref, cwd=backend)
        run_git("checkout", "--detach", "FETCH_HEAD", cwd=backend)

    if not dry_run and not (backend / "deploy" / "modal_app.py").is_file():
        raise FileNotFoundError(f"Backend entrypoint missing: {backend / 'deploy' / 'modal_app.py'}")
    return backend


def build_command(action: str) -> tuple[list[str], dict[str, str]]:
    if action not in {"deploy", "serve"}:
        raise ValueError(f"Unsupported action: {action}")
    environment = os.environ.copy()
    environment["QWEN38_PI_LORA"] = "1"
    environment.setdefault("QWEN38_SPECULATIVE_ALGORITHM", "NONE")
    return ["uv", "run", "modal", action, "deploy/modal_app.py"], environment


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=("deploy", "serve"), default="deploy")
    parser.add_argument("--backend-path", type=Path, help="Use an existing local checkout")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--ref", default=DEFAULT_REF, help="Git branch to clone (default: main)")
    parser.add_argument("--update-backend", action="store_true", help="Fetch and use latest selected ref")
    parser.add_argument("--dry-run", action="store_true", help="Show planned call without Git or Modal changes")
    args = parser.parse_args()

    backend = resolve_backend(
        backend_path=args.backend_path,
        cache_dir=args.cache_dir,
        ref=args.ref,
        update=args.update_backend,
        dry_run=args.dry_run,
    )
    command, env = build_command(args.action)
    print(f"Source:  {REPOSITORY}")
    print(f"Backend: {backend}")
    print(f"Model:   {MODEL_NAME}")
    print(f"Command: {' '.join(command)}")
    print(f"LoRA:    {env['QWEN38_PI_LORA']}")
    print(f"Draft:   {env['QWEN38_SPECULATIVE_ALGORITHM']}")
    if not args.dry_run:
        print(f"Commit:  {run_git('rev-parse', 'HEAD', cwd=backend)}")
        subprocess.run(command, cwd=backend, env=env, check=True)


if __name__ == "__main__":
    main()
