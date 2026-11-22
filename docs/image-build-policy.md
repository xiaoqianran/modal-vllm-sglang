# Modal image build policy

The three model services use immutable Docker image manifest digests. These
digests were read from the Docker Hub registry on 2026-11-21; they were **not**
verified by a new remote GPU inference test. Existing Modal deployments are
not changed by editing these local JSON files.

| Service | Engine image | Local installation policy |
| --- | --- | --- |
| MOSS | `vllm/vllm-omni` pinned from `v0.32.0` | No additional build dependencies |
| Pika | `lmsysorg/sglang` pinned from `dev` | No additional build dependencies |
| MiniMax H3 Image | Existing pinned SGLang base image | `uv_pip_install` using Modal's default uv and the existing pinned SGLang Git revision |

## uv builds

`runtime/serving/modal_setup.py` delegates package installation to Modal's
native `Image.uv_pip_install`, which automatically provides uv in the
builder and installs into the first Python on `PATH`. The existing SGLang
base image previously used `python -m pip` to install into
`/opt/sglang/lib/python3.12/site-packages`; before deploying MiniMax, verify
that the first Python on `PATH` still targets this environment.

`uv_packages` is an optional array of requirement strings in `model.json`.
No uv executable version is set; Modal manages its default. Legacy
`build_commands` remain available for non-Python custom build actions.
When `uv_packages` is absent, no uv installation layer is created.

uv can improve resolution, fetching and installing packages, but it **does not
eliminate** Git checkout, SGLang source-wheel compilation, Docker layer pulls,
or image saving. The slow first build should be measured against a subsequent
identical cached build before claiming a speedup.

## Safe rollout

1. Run `uv run python -m unittest discover -s tests -q` locally.
2. Compare each pinned digest with the corresponding previously deployed image.
3. When remote tasks are no longer active, deploy one service at a time.
4. Confirm engine startup and execute one minimal inference per model before
   treating the new images as validated.

No build, deploy, or GPU call is required to run the offline tests.
