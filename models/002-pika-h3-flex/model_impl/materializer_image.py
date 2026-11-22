"""CPU-only SGLang Overlay publisher, separate from the GPU engine image.

Do not construct this image until benchmark has verified every input snapshot.
The SGLang revision is fixed and its Pika registry contract is checked again
inside this model_impl package before publishing any ready marker.
"""
from pathlib import Path

import modal


ROOT = Path(__file__).resolve().parents[1]
SHARED_ROOT = ROOT.parents[1] / "runtime"

# Reuse the project's existing pinned SGLang source revision rather than
# installing an unbounded prerelease that could silently change the overlay
# manifest contract. The pinned revision still needs a real cloud build test.
SGLANG_MATERIALIZER = (
    "sglang[diffusion] @ git+https://github.com/sgl-project/sglang.git"
    "@a9d721f36e16dab1835de0203b620256da1e612d#subdirectory=python"
)


def build_pika_materializer_image(spec, *, modal_version):
    """Define an uv-installed Python image without pulling lmsysorg/sglang."""
    if spec["service"] != "pika" or spec.get("cache", {}).get("mode") != "sglang_overlay":
        raise ValueError("Only Pika Overlay may use the CPU materializer")
    root = spec["cache_directory"]
    image = (
        modal.Image.debian_slim(python_version="3.12")
        .apt_install("git")
        .uv_pip_install(
            f"modal[api-proxy-support]=={modal_version}",
            SGLANG_MATERIALIZER,
            "huggingface_hub>=0.30,<2",
        )
        .env({
            "PYTHONPATH": "/opt",
            "HF_HOME": root,
            "SGLANG_DIFFUSION_CACHE_ROOT": root + "/sglang",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        })
    )
    return (image
            .add_local_dir(str(ROOT / "model_impl"), "/opt/models/" + ROOT.name + "/model_impl", copy=False)
            .add_local_dir(str(SHARED_ROOT), "/opt/runtime", copy=False))
