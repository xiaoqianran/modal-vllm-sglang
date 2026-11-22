"""Offline contract tests; never start a GPU or fetch model weights."""
import asyncio
from pathlib import Path, PurePosixPath
from runpy import run_path
from unittest.mock import patch

import pytest

from shared.config.model_spec import validate, discover
from shared.config.model_cache import command_argv, component_specs
from shared.serving.llm_auth import BearerAuth
from shared.utils.files import read_json


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "006-qwen38-pi" / "model.json"


@pytest.mark.parametrize("path, authorization, expected", [
    ("/v1/chat/completions", b"", 401),
    ("/invocations", b"Bearer incorrect", 401),
    ("/invocations", b"", 401),
    ("/v1/models", b"Bearer test-key", 200),
])
def test_bearer_auth_applies_to_all_http_routes(monkeypatch, path, authorization, expected):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    events = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def send(event):
        events.append(event)

    scope = {"type": "http", "path": path, "headers": [(b"authorization", authorization)]}
    asyncio.run(BearerAuth(app)(scope, None, send))
    assert events[0]["status"] == expected


def test_llm_backend_entrypoint_uses_latest_external_checkout():
    entry = run_path(str(CONFIG.with_name("app.py")))
    assert entry["DEFAULT_REF"] == "main"
    assert entry["REPOSITORY"].endswith("/Qwen3.8-27B-RTX-6000-PRO-SGLang-DSpark.git")
    command, environment = entry["build_command"]("deploy")
    assert command == ["uv", "run", "modal", "deploy", "deploy/modal_app.py"]
    assert environment["QWEN38_PI_LORA"] == "1"
    assert environment["QWEN38_SPECULATIVE_ALGORITHM"] == "DFLASH"


def test_llm_spec_and_components():
    spec = validate(read_json(CONFIG))
    assert spec["kind"] == "llm"
    assert spec["operation"] == "chat"
    assert set(component_specs(spec)) == {"model", "adapter"}
    assert spec["components"]["adapter"]["model"] == "harness-hub/Qwen3.8-27B-pi-adapted"


def test_llm_command_injects_local_base_and_adapter():
    spec = validate(read_json(CONFIG))
    args = command_argv(spec, {"model": PurePosixPath("/mnt/modal/base"),
                               "adapter": PurePosixPath("/mnt/modal/adapter")})
    assert args[:3] == ["vllm", "serve", "/mnt/modal/base"]
    assert args[args.index("--served-model-name") + 1] == "qwen3.8-27B"
    assert args[args.index("--lora-modules") + 1] == "qwen3.8-27B-pi=/mnt/modal/adapter"
    assert args[args.index("--tool-call-parser") + 1] == "qwen3_xml"
    assert args[args.index("--max-model-len") + 1] == "131072"
    assert args[args.index("--default-chat-template-kwargs") + 1] == '{"enable_thinking":false}'


def test_llm_not_exposed_as_media_task():
    discovered = discover(ROOT)
    assert all(not name.startswith("qwen38-pi.") for name in discovered)
    assert any(name.startswith("moss.") for name in discovered)


def test_invalid_llm_operation_rejected():
    spec = read_json(CONFIG)
    spec["operation"] = "t2i"
    with pytest.raises(ValueError, match="chat"):
        validate(spec)


def test_llm_runtime_rejects_media_configuration():
    from shared.serving.llm_runtime import create_llm_app
    with pytest.raises(ValueError, match="LLM"):
        with patch("shared.serving.llm_runtime.read_json") as read:
            read.return_value = read_json(ROOT / "001-moss-image" / "model.json")
            create_llm_app(ROOT / "001-moss-image" / "model.json")
