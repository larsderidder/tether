"""New sessions inherit only models allowed by the current configuration."""

from pathlib import Path

import httpx
import pytest
from agent_tether.base import BridgeConfig

from tether.bridges.telegram.bot import TelegramBridge


@pytest.mark.parametrize(
    "parent_model, available_models, blocked_models, expected",
    [
        ("openai-codex/gpt-6-astra", "openai/gpt-6-astra", "", None),
        ("openai/gpt-6-astra", "openai/gpt-6-astra", "*/gpt-6-astra", None),
        ("openai/gpt-6-astra", "openai/gpt-6-astra", "", "openai/gpt-6-astra"),
        ("gpt-6-astra", "openai/gpt-6-astra", "", "openai/gpt-6-astra"),
        ("provider/custom", "", "", "provider/custom"),
        (None, "openai/gpt-6-astra", "", None),
    ],
)
def test_child_model_respects_current_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parent_model: str | None,
    available_models: str,
    blocked_models: str,
    expected: str | None,
) -> None:
    """Unavailable or blocked inherited models defer to the new-session default."""
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path))
    monkeypatch.setenv("TETHER_PI_DEFAULT_MODEL", "")
    monkeypatch.setenv("TETHER_PI_MODELS", available_models)
    monkeypatch.setenv("TETHER_PI_BLOCKED_MODELS", blocked_models)
    monkeypatch.delenv("TETHER_PI_MODEL_BLACKLIST", raising=False)
    bridge = TelegramBridge(
        bot_token="test_token",
        forum_group_id=-1001234567890,
        config=BridgeConfig(data_dir=str(tmp_path)),
        get_session_info=lambda _session_id: {
            "adapter": "pi_rpc",
            "model": parent_model,
        },
    )

    assert bridge._model_for_child_session("sess_parent", "pi") == expected


@pytest.mark.anyio
async def test_stale_parent_model_creates_session_with_default(
    api_client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale provider prefix must not prevent creation or change the default."""
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path))
    monkeypatch.setenv("TETHER_PI_DEFAULT_MODEL", "openai/gpt-6.1-sol")
    monkeypatch.setenv("TETHER_PI_MODELS", "openai/gpt-6.1-sol,openai/gpt-6-astra")
    monkeypatch.delenv("TETHER_PI_BLOCKED_MODELS", raising=False)
    monkeypatch.delenv("TETHER_PI_MODEL_BLACKLIST", raising=False)
    bridge = TelegramBridge(
        bot_token="test_token",
        forum_group_id=-1001234567890,
        config=BridgeConfig(data_dir=str(tmp_path)),
        get_session_info=lambda _session_id: {
            "adapter": "pi_rpc",
            "model": "openai-codex/gpt-6-astra",
        },
    )
    model = bridge._model_for_child_session("sess_parent", "pi")

    response = await api_client.post(
        "/api/sessions",
        json={"directory": str(tmp_path), "adapter": "pi", "model": model},
    )

    assert response.status_code == 201
    assert response.json()["model"] == "openai/gpt-6.1-sol"
