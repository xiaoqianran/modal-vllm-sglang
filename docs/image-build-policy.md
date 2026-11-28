# Modal image build policy

Prefer Modal-native images (`debian_slim` + `uv_pip_install`) and pinned
published wheels for new models. Use a third-party registry image only when
native installation cannot satisfy the engine's dependencies or runtime
validation. Pin registry alternatives to an immutable manifest digest.

| Service | Base image | Installation |
| --- | --- | --- |
| MOSS | Modal Debian Slim, Python 3.12 | vLLM/Omni 0.32.0, CUDA backend cu130; prefetch vLLM kernels |
| Pika | Modal Debian Slim, Python 3.12 | Verified official SGLang nightly wheel, CUDA wheel links, FFmpeg |
| MiniMax H3 Image | Modal Debian Slim, Python 3.12 | Same verified SGLang nightly wheel and CUDA wheel links |
| Qwen3.8 Pi | External SGLang backend; archived vLLM spec unchanged | Native wheel import/CLI audit passed; model store unavailable for GPU test |

MOSS native configuration follows the upstream [vLLM-Omni 0.32.0 release](https://github.com/vllm-project/vllm-omni/releases/tag/v0.32.0).
The original registry baseline is
`vllm/vllm-omni@sha256:f713b909f16cf8c7901f17c3aee3f471cbee5fbde5603b7dbe2631630887d7c2`.
A source change does not alter existing deployed apps.

MOSS configuration now pins vLLM/Omni 0.34.0 as an unverified upgrade candidate. The 0.32.0 measurements below remain historical. MiniMax Omni is a separate candidate service using 0.34.0, a local encoder view, and layer offload; GPU acceptance is pending.

## Native image configuration

```json
{
  "image": "debian_slim",
  "python_version": "3.12",
  "uv_packages": ["vllm==0.32.0", "vllm-omni==0.32.0"],
  "uv_torch_backend": "cu130",
  "build_commands": ["flashinfer install-cubin-wheel", "flashinfer install-jit-cache-wheel --cuda-version 13.0 --mode minimal --sm sm120"]
}
```

The base image and explicit Python minor version are separate from the model
weights. The shared builder uses Modal's native uv installer, without adding
a separate uv dependency. `uv_torch_backend` is optional; when provided it
selects an explicit CUDA backend and requires `uv_packages`.
`build_commands` run after package installation and are for additional
model-specific system dependencies and compatibility actions. MOSS prefetches FlashInfer cubins and only the JIT cache provider for its configured RTX PRO 6000 (SM120) on the CPU builder. Direct FlashInfer CLI commands avoid vLLM CLI device detection. When changing GPU architectures, review the SM target.
Sources are mounted last with `copy=False`.

Registry configurations continue to use `Image.from_registry` and may
specify `uv_packages`. Registry Python versions are determined by the base.
Existing SGLang source-wheel builds retain their Rust extension build flag.

Native installation avoids importing a large external engine image, but
CUDA/PyTorch wheels, kernel prefetch, dependency resolution, image saving
and Modal caching still affect the build. Modal's installer may itself import
a small uv utility image; eliminating the engine import is not a guarantee
that no OCI-related log lines will ever appear.
Keep model weights in the existing Volume; changing an image does not
require downloading the weights again.

## Locked engine dependencies (Linux / CUDA)

MOSS and MiniMax Omni reference `runtime/serving/locks/vllm-omni-0.34.0-cu130.txt` through `uv_requirements`. The model specifications retain their top-level engine pins. The shared builder pins uv 0.12.5 (verified official registry tag), rejects missing or stale locks and installs with `uv_pip_install(requirements=...)`, `--require-hashes`, the official PyPI index and explicit CUDA backend. Lock changes affect the deployment fingerprint of models that reference the lock.

`uv_only_binary` accepts `true` (all packages) or a list of critical binary packages. Omni restricts vLLM, vLLM-Omni, PyTorch, FlashInfer and Triton to wheels. The existing pure-Python openai-whisper source distribution is permitted. Pika and MiniMax SGLang reference the shared `sglang-20261124-cu130.txt` lock and retain the binary-only policy.

To refresh the Omni lock, create a temporary engine.in containing vllm==0.34.0, vllm-omni==0.34.0 and flashinfer-python==0.7.3 (one requirement per line). Resolve for the remote platform, not Windows:

```bash
uv pip compile --no-config --python-version 3.12 \
  --python-platform x86_64-manylinux_2_39 --torch-backend cu130 \
  --index-url https://pypi.org/simple \
  --only-binary vllm,vllm-omni,torch,flashinfer-python,triton \
  --generate-hashes --no-header --no-annotate engine.in \
  -o runtime/serving/locks/vllm-omni-0.34.0-cu130.txt
```

To refresh the SGLang lock, use the existing official direct Wheel requirement from the model's uv_packages as the input, add --extra-index-url https://docs.sglang.ai/whl/cu130/, replace the binary package list with --only-binary=:all:, and output sglang-20261124-cu130.txt. Keep the direct Wheel URL and its SHA-256 fragment intact.

On engine upgrades, use a new lock filename and update both model configurations. CUDA setup commands occupy separate layers so model-specific system dependencies do not invalidate shared prerequisites. Runtime source remains mounted last. The locked recipe requires real image-build and GPU acceptance before production rollout. Hashes authenticate dependency distributions, not isolated source-build outputs or every runtime-generated kernel.

## Safe rollout

1. Run `uv run python -m unittest discover -s tests -q`.
2. Build the native image in an ephemeral Modal app without replacing production.
3. Verify installed package versions, CUDA compatibility, engine health and
   real generation using the existing model cache. Compare against the pinned
   registry baseline using identical GPU, input and model fingerprint.
4. Test a new GPU memory snapshot and subsequent restore before production
   rollout; do not reuse the old deployment's snapshot.
5. Deploy one service at a time when no active remote work would be interrupted.

If native installation cannot pass the required tests, retain the pinned
registry baseline until the failure is understood and resolved.

## Verified native builds (2026-11-24)

### MOSS Image — GPU generation verified

- Build: Modal Debian Slim / Python 3.12, vLLM and vLLM-Omni 0.32.0,
  CUDA 13.0; FlashInfer 0.7.0.post1 cubin and SM120-family JIT cache.
- Native image ID: `im-juhvjUTgxUwuck7NUsTSqH`. Cold dependency and kernel
  layers took 65.60 s and 89.37 s in separate builds; environment layer 7.55 s.
  Their sum is not a controlled single-run cold build measurement.
- Ephemeral GPU test reused the existing `model-store` cache, fingerprint
  `43e836eac25c9650d16b4f5f12999d2aa3f0237d820b64f6b11e03d396e3cf2c`.
  RTX PRO 6000, seed 42, 1024x1024, 50 steps; engine time 14.66 s,
  3.48 step/s, model loading 25.3364 GiB, 32.13 s. Engine initialization
  73.65 s, gateway initialization 3.04 s. HTTP 200; PNG decoded and
  its dimensions and SHA-256 verified against the published record.
- Output: `validation/moss-native-1705ea226874/001.png` (3,147,861 bytes),
  SHA-256 `0217fbc47851812753c492de9a5b747e8dd5981b6e611ea28330f21c923bd9e5`.
- [Modal run](https://modal.com/apps/zhangchenzer/main/ap-1u8PY44KRR8verwtsFLpPJ).
- Warm inference matches the historical 14.66 s engine result but the runs
  are not a controlled A/B test. Ephemeral apps disable GPU snapshots;
  production snapshot/restore and cold registry comparison remain unverified.
- Optional causal-conv1d and flash-linear-attention reported reference fallback.
  Do not add a speculative incompatible binary or claim that all runtime JIT
  has been removed. The current tested generation latency is already comparable.

### SGLang — official binary-only dependency image verified

- [Stable PyPI release](https://pypi.org/project/sglang/): 0.5.23 (2026-10-30).
- [Official nightly release](https://github.com/sgl-project/whl/releases/tag/nightly-2026-11-24-1173264b58):
  `0.5.24.dev20261124+g1173264b58`, CPython 3.12, x86_64 manylinux_2_39,
  46,280,354 bytes, SHA-256 `3c4e8dfb13c18bf3bf685f9029c0143a9ae74fe5c4124e7076f558f0ed2e6596`.
- Native dependency layer `im-TTxkkREqHet2Bz4lJr5LoV` built in 133.16 s;
  metadata-verification image `im-h8qVE5pIRzWHrAoTKZoCSF` built in 6.09 s;
  complete client wall time 152.42 s. uv resolved 235 packages, installed 234
  via wheels with `--only-binary=:all:` (no source distribution builds).
- Installed `torch==2.14.1+cu130`, `sglang-kernel==0.4.11+cu130`,
  `sgl-deep-gemm==0.2.9+cu130`, `sgl-fa3==0.1.0+cu130`,
  `sgl-flash-mla==1.0.5+cu130`, `sgl-deep-ep==0.1.3+cu130`,
  `flashinfer-python==0.7.4`. The installed wheel contains MiniMax H3 Image
  pipeline configs and tests, plus the Pika Flex test module.
- The previous MiniMax Git pin is on a divergent history. Do not infer model
  or overlay compatibility from a successful package build. Existing Pika
  and MiniMax cache manifests passed preflight without downloading weights.
- [Installation guide](https://docs.sglang.ai/docs/get-started/install),
  [official CUDA wheel index](https://docs.sglang.ai/whl/cu130/).

The shared builder supports optional `uv_extra_index_url` (HTTPS wheel index)
and `uv_only_binary: true` (reject source distributions). For official Nightly
wheels, use an immutable direct URL with its SHA-256 fragment, not floating
`--pre` or `git+main`. Strict binary mode is opt-in: MOSS has a pure-Python
openai-whisper source distribution but no observed vLLM/PyTorch CUDA source build.
Use the same dependency recipe for Pika and MiniMax so Modal can share cached
layers; preserve their model-specific command, cache and request contracts.
Pika CPU overlay materialization is a separate compatibility boundary and is
not automatically replaced by a GPU engine-wheel test.

### SGLang native recipe

The two GPU services use the same verified dependency layer. The following
fields show their common installation recipe; the model.json files also include
the CUDA library layout fix below, plus FFmpeg for Pika only. These are the
default repository configurations, not an approval to replace live deployments.

```json
{
  "image": "debian_slim",
  "python_version": "3.12",
  "uv_packages": [
    "sglang[diffusion] @ https://github.com/sgl-project/whl/releases/download/nightly-2026-11-24-1173264b58/sglang-0.5.24.dev20261124%2Bg1173264b58-cp312-cp312-manylinux_2_39_x86_64.whl#sha256=3c4e8dfb13c18bf3bf685f9029c0143a9ae74fe5c4124e7076f558f0ed2e6596"
  ],
  "uv_torch_backend": "cu130",
  "uv_extra_index_url": "https://docs.sglang.ai/whl/cu130/",
  "uv_only_binary": true,
  "env": {
    "CUDA_HOME": "/usr/local/lib/python3.12/site-packages/nvidia/cu13"
  }
}
```

The first Pika GPU attempt failed during the DeepGEMM import because CUDA_HOME
was absent. MiniMax's generic CLI selected the LLM fallback instead of the
diffusion backend. A CPU inspection of the exact native dependency image
confirmed NVIDIA wheel-provided nvcc, CUDA headers and libcudart at the
CUDA_HOME above; no external CUDA toolkit image is needed for path discovery.
Use the explicit diffusion CLI
`python -m sglang.multimodal_gen.runtime.entrypoints.cli.main serve`
to expose backend startup failures instead of silently falling back to LLM.
Keep the CUDA path coupled to the selected Python minor version and verified
wheel layout. Do not assume arbitrary GPU architectures have the same kernels.

Audit: [CUDA wheel inspection](https://modal.com/apps/zhangchenzer/main/ap-01nGkMicgQRCHJtA5ua0EU).

### SGLang runtime findings

- [Pika initial attempt](https://modal.com/apps/zhangchenzer/main/ap-834xmrucl6IiQ3cetidaop)
  and [MiniMax initial attempt](https://modal.com/apps/zhangchenzer/main/ap-67ZRta1iPfitPLw8ae1iTA)
  did not pass startup. Subsequent corrected runs are recorded below.
- [Pika CUDA-path retest](https://modal.com/apps/zhangchenzer/main/ap-TRyiHdBYTN4CQfM31zfHjt)
  recognized `PikaH3FlexPipeline` and initialized its CUDA worker, but the
  pipeline requires system ffmpeg and ffprobe. Pika-only build command:
  `apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*`.
  A separate [CPU media-image test](https://modal.com/apps/zhangchenzer/main/ap-9vQ4AcE84LexoRezfF0L3y)
  installed and executed both ffmpeg and ffprobe 7.1.5 successfully. The
  corrected full Pika GPU pipeline passed in the follow-up run below. These media
  executables are not GPU kernels and should not be added to every image
  without need.
- [MiniMax CUDA-path retest](https://modal.com/apps/zhangchenzer/main/ap-WpFpJ2rI0bYu5ljOFKWq0K)
  loaded all components and returned HTTP 200 on engine and gateway health
  (engine 195.67 s, end-to-end startup 198.68 s, no snapshot). The fused
  QKNorm+RoPE JIT kernel attempted nvcc compilation and failed to link
  `-lcudart`: the wheel has `lib/libcudart.so.13`, while that build searched
  `lib64`. The real 1024x1024, six-step request failed with HTTP 500 at the
  required QKNorm JIT kernel. No image was produced.
  A separate [CPU CUDA linker audit](https://modal.com/apps/zhangchenzer/main/ap-FlRm9ZI528wLeF8Y5Ac57W)
  verified that the original wheel has neither lib64 nor the unversioned
  libcudart.so link. Creating both links allowed c++ to compile, link and
  execute a small CUDA runtime version probe without downloading a toolkit.
  Build command for this exact Python/CUDA wheel layout:
- Targeted offline regression: 11 image-policy and 16 service tests passed;
  full unittest discovery did not finish within a 90-second execution cap.

```sh
set -e
CUDA_ROOT=/usr/local/lib/python3.12/site-packages/nvidia/cu13
test -f "$CUDA_ROOT/lib/libcudart.so.13"
test -e "$CUDA_ROOT/lib64" || ln -s lib "$CUDA_ROOT/lib64"
test -e "$CUDA_ROOT/lib/libcudart.so" || ln -s libcudart.so.13 "$CUDA_ROOT/lib/libcudart.so"
```

The linker probe and both corrected real GPU generations passed. Treat the
links as a targeted compatibility fix, not proof that all kernel compilation
has been eliminated. Some specialized kernels still use runtime JIT; retain
published precompiled kernels where available. Both model.json files now
select the verified native image recipe. GPU snapshot capture/restore and
full production workload acceptance remain deployment gates.

### Corrected native GPU acceptance (2026-11-24)

Both tests reused the existing model-store cache, used an RTX PRO 6000 and
disabled snapshots. Pika also disabled startup warmup for this smoke test;
its existing default warmup policy was retained in the repository config.
Both images share the exact immutable SGLang wheel and CUDA library-layout
fix. Pika alone installs FFmpeg/ffprobe.

| Metric | MiniMax H3 Image | Pika H3 Flex |
| --- | --- | --- |
| Engine initialization | 177.54 s | 111.61 s |
| Full service initialization | 180.55 s | 114.62 s |
| First generation (engine) | 60.26 s | 65.00 s |
| Output | 1024x1024 PNG, six steps | 864x480 H.264 + AAC MP4, 5.167 s, six steps |
| Output validation | Decoded PNG and SHA-256 | FFprobe video/audio streams and SHA-256 |
| Result | PASS | PASS |

- [MiniMax GPU run](https://modal.com/apps/zhangchenzer/main/ap-qkyAXMiJ0xGbGhsmddgLZT).
  Output: validation/minimax-native-fixed-ab64351b1664/001.png,
  3,147,060 bytes, SHA-256 65667aca50d644711dbd57ba98844da005c1fd9b726b2a666d9a0f2958cd6a6e.
- [Pika GPU run](https://modal.com/apps/zhangchenzer/main/ap-iKgq1lcoFaMCSgYknu8RDQ).
  Output: validation/pika-native-fixed-c52dd6dc22e3/001.mp4,
  728,698 bytes, SHA-256 6465b36f6ee52e915899c285af48ac63268ee4416bb35a6502330c1b34b87f3f.
- After updating the two model.json files, 10 image-policy tests and 16
  model-service tests passed. Pika's CPU Overlay materializer keeps its
  separately pinned source implementation; the GPU test validated loading
  existing materialized weights, not rebuilding the Overlay.
- These are first-request smoke tests, not controlled performance comparisons,
  full-resolution/first-last-frame tests, or snapshot restore tests. Live
  deployments were not replaced.

### Qwen3.8 Pi external backend

The active deployment lives in the separate
[Qwen3.8 SGLang backend](https://github.com/xiaoqianran/Qwen3.8-27B-RTX-6000-PRO-SGLang-DSpark).
The native nightly image passed all four DFlash2 runtime import checks and
all seven checked CLI flags in a [CPU audit](https://modal.com/apps/zhangchenzer/main/ap-KQwW0o8XpX9YWGeiL0jaJM).
The [read-only model preflight](https://modal.com/apps/zhangchenzer/main/ap-HFJqCNNREM9ak2LhpGcWhi)
could not find qwen38-27b-model-store in the current main environment. No
weights were downloaded and no Qwen GPU was allocated. The external
backend's validated DFlash2/Pi LoRA image selection and the archived
vLLM model.json remain unchanged.

### Three-model native deployment acceptance (2026-11-25)

After upgrading modal-benchmark to 0.4.2 (6844a17), all three media services
passed three real GPU generations each on RTX PRO 6000 using the native
Debian Slim / Python 3.12 image recipes and existing model-store weights.
MOSS reused its verified deployment; Pika and MiniMax were deployed or
updated after their deployment signatures were checked. No model weights
were downloaded and no outputs were archived to Hugging Face.

| Service | Execution 1 (s) | Execution 2 (s) | Execution 3 (s) | Client wall 1 (s) | Client wall 2 (s) | Client wall 3 (s) | Result |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| moss | 15.49 | 15.17 | 15.11 | 125.82 | 27.09 | 21.51 | 3/3 PASS |
| pika | 25.54 | 25.36 | 25.98 | 320.92 | 32.38 | 31.40 | 3/3 PASS |
| minimax | 68.49 | 7.33 | 7.52 | 296.61 | 18.33 | 17.65 | 3/3 PASS |

- MOSS: three 1024x1024 PNG images, 50 steps.
- MiniMax: three 2048x1152 PNG images, six steps.
- Pika: three 864x480 MP4 videos, six steps, requested five seconds;
  actual duration 5.167 s, H.264 at 24 FPS and AAC at 32 kHz.
- Nine independent prompts with fixed seeds; all nine local outputs matched
  the remote SHA-256 and byte counts. All six PNGs decoded with Pillow; all
  three video and audio streams decoded end-to-end with FFmpeg.
- Temporary PC2 outputs and per-request receipts: `outputs/temp-smoke-042-20261125/`.
  Generated artifacts remain local and are not part of this commit.
- Execution time includes generation and server-side result persistence;
  client wall time also includes scheduling, initialization and transfer.
  The first request is not an isolated cold-start measurement. GPU snapshots
  were enabled and the scaledown window was 30 s; independent snapshot
  restore timing and subjective output quality were not evaluated.
- Acceptance applies to the deployments used in this run, not subsequent
  uncommitted runtime changes or a full cold-cache model download.
