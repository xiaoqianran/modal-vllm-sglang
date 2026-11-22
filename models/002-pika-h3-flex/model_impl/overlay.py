"""Pika Overlay bridge: benchmark downloads files, SGLang materializes them.

The SGLang custom materializer is the authority on the model layout.
This adapter only supplies its already-downloaded, immutable inputs.
"""
from pathlib import Path
from unittest.mock import patch
import json

from runtime.config.model_cache import checked_inventory, component_specs, verify_cache
from runtime.tasks.task_identity import intent_digest
from runtime.utils.files import atomic_json

SOURCE_REPO = "Pika-Labs/Pika-H3-Flex"
SOURCE_REV = "3ff166447d5edb1406107ab8370c42000ce173e4"
OVERLAY_REPO = "Niehen6174/Pika-H3-Flex-overlay"
OVERLAY_REV = "2d8d5e4d81d2ad0ba4b0bbcf7049231bfbe3e9c2"
BASE_REPO = "MiniMaxAI/MiniMax-H3"
BASE_REV = "42ed227ee7df40d41602854ae760620d6eb651fe"

COMPONENTS = ("text_encoder", "tokenizer", "processor", "video_vae", "audio_vae", "transformer")
PARTITIONS = ("FL2VA", "Ref2VA")
SOURCE_PATTERNS = ("fl2va/*", "ref2va/*", "README.md", "LICENSE", "NOTICE")
OVERLAY_PATTERNS = ("*.json", "*.md", "*.py", "*.txt",
                    "**/*.json", "**/*.md", "**/*.py", "**/*.txt")
BASE_PATTERNS = tuple(f"{partition}/{component}/**"
                      for partition in PARTITIONS for component in COMPONENTS)


def download_sources(spec):
    """Fixed file sets, never the full ~200GB Pika Diffusers export."""
    if spec["model"] != SOURCE_REPO or spec.get("components"):
        raise ValueError("This overlay adapter supports only Pika-H3-Flex")
    return [
        {"repo_id": SOURCE_REPO, "revision": SOURCE_REV, "include": ",".join(SOURCE_PATTERNS)},
        {"repo_id": OVERLAY_REPO, "revision": OVERLAY_REV, "include": ",".join(OVERLAY_PATTERNS)},
        {"repo_id": BASE_REPO, "revision": BASE_REV, "include": ",".join(BASE_PATTERNS)},
    ]


def _downloaded_models(manifest, base, root):
    base, root = Path(base), Path(root)
    if not base.resolve().is_relative_to(root.resolve()) or base.resolve() == root.resolve():
        raise ValueError("Downloaded snapshots must stay inside the model cache")
    models = {model["repo_id"]: model for model in manifest["models"]}
    expected = {SOURCE_REPO: SOURCE_REV, OVERLAY_REPO: OVERLAY_REV, BASE_REPO: BASE_REV}
    if len(manifest["models"]) != 3 or set(models) != set(expected):
        raise ValueError("Pika requires exactly source, overlay and base repositories")
    paths = {}
    for repo, revision in expected.items():
        model = models[repo]
        if model["revision"] != revision:
            raise ValueError(f"Pinned Pika revision mismatch: {repo}")
        path = (base / repo / revision / "model").resolve()
        if not path.is_relative_to(base.resolve()):
            raise ValueError("Snapshot path escapes base")
        # Compare each pinned file; HF snapshots may legitimately contain
        # zero-byte metadata files, unlike the final inference-cache contract.
        for name, meta in model["files"].items():
            file = (path / name).resolve()
            if not file.is_relative_to(path):
                raise ValueError("Pika snapshot filename escapes its repository")
            if not file.is_file() or file.stat().st_size != meta["size"]:
                raise RuntimeError(f"Persisted Pika snapshot incomplete: {repo}/{name}")
        paths[repo] = path
    return paths


def _verify_source_contract(source, overlay):
    manifest_file = overlay / "_overlay/overlay_manifest.json"
    config = json.loads(manifest_file.read_text(encoding="utf-8"))
    if (config.get("source_model_id") != SOURCE_REPO
            or config.get("source_revision") != SOURCE_REV
            or config.get("custom_materializer") != "_overlay/materialize.py"):
        raise ValueError("SGLang overlay manifest is not the pinned Pika release")
    for rel in config.get("required_source_files", []):
        if not (source / rel).is_file():
            raise RuntimeError(f"Pika source is missing {rel}")
    for partition in ("fl2va", "ref2va"):
        data = json.loads((source / partition / "manifest.json").read_text(encoding="utf-8"))
        base = data.get("base", {})
        if base.get("repo_id") != BASE_REPO or base.get("revision") != BASE_REV:
            raise ValueError("Pika adapter does not pin the downloaded base revision")


def materialize_with_sglang(source_dir, overlay_dir, base_dir):
    """Call upstream SGLang, intercept only its pinned base snapshot request."""
    import huggingface_hub
    from sglang.multimodal_gen.runtime.utils import hf_diffusers_utils, model_overlay

    target = model_overlay.resolve_model_overlay(SOURCE_REPO)
    if (not target or target.get("overlay_repo_id") != OVERLAY_REPO
            or target.get("overlay_revision") != OVERLAY_REV
            or target.get("source_revision") != SOURCE_REV):
        raise RuntimeError(
            "Installed SGLang overlay registry differs from pinned Pika inputs: "
            f"observed={target!r}, expected_overlay={OVERLAY_REPO}@{OVERLAY_REV}, "
            f"expected_source={SOURCE_REV}"
        )

    def local_base(repo_id, **kwargs):
        if (repo_id != BASE_REPO or kwargs.get("revision") != BASE_REV
                or set(kwargs.get("allow_patterns", [])) != set(BASE_PATTERNS)):
            raise RuntimeError("Unexpected SGLang snapshot request; refusing network fallback")
        return str(base_dir)

    # The pinned Pika materializer imports snapshot_download at call time.
    # Only the expected MiniMax H3 request can resolve; nothing is redownloaded.
    with patch.object(huggingface_hub, "snapshot_download", local_base):
        return Path(model_overlay.materialize_overlay_model(
            source_model_id=SOURCE_REPO, overlay_spec=target,
            overlay_dir=str(overlay_dir), source_dir=str(source_dir),
            source_revision=SOURCE_REV,
            verify_diffusers_model_complete_fn=hf_diffusers_utils._verify_diffusers_model_complete,
        ))


def publish_pika_cache(spec, manifest, base, root, volume, *, materialize=materialize_with_sglang):
    """Publish the old inference cache contract only after complete materialization."""
    paths = _downloaded_models(manifest, base, root)
    _verify_source_contract(paths[SOURCE_REPO], paths[OVERLAY_REPO])
    model_dir = Path(materialize(paths[SOURCE_REPO], paths[OVERLAY_REPO], paths[BASE_REPO])).resolve()
    root = Path(root).resolve()
    expected_root = root / "sglang/materialized_models"
    if not model_dir.is_relative_to(expected_root) or model_dir == expected_root:
        raise ValueError("SGLang materialized outside the service cache")
    marker = model_dir / ".sglang_overlay_materialized.json"
    if not marker.is_file():
        raise RuntimeError("SGLang materializer did not publish its completion marker")
    evidence = json.loads(marker.read_text(encoding="utf-8"))
    if (evidence.get("source_model_id") != SOURCE_REPO
            or evidence.get("source_revision") != SOURCE_REV
            or evidence.get("overlay_repo_id") != OVERLAY_REPO
            or evidence.get("overlay_revision") != OVERLAY_REV):
        raise RuntimeError("SGLang materialization marker does not match pinned inputs")
    inventory = checked_inventory(model_dir, component_specs(spec)["model"])
    record = {"version": 1, "cache_spec": intent_digest(component_specs(spec)),
              "components": {"model": {
                  "path": model_dir.relative_to(root).as_posix(),
                  "resolved_revision": SOURCE_REV,
                  "inventory": inventory}}}
    _, fingerprint = verify_cache(spec, root, phase="download_pika_prepare", record=record)
    atomic_json(root / "model-ready.json", record)
    volume.commit()
    return {"ready": True, "fingerprint": fingerprint, "path": str(model_dir)}
