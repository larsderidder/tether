"""Pi's saved model choices take precedence over legacy Tether settings."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from tether.settings import Settings


@pytest.fixture
def pi_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Use isolated Pi settings alongside a stale Tether model configuration."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.setenv("TETHER_DEFAULT_AGENT_ADAPTER", "pi")
    monkeypatch.setenv("TETHER_PI_DEFAULT_MODEL", "openai-codex/gpt-6-astra")
    monkeypatch.setenv("TETHER_PI_MODELS", "openai-codex/gpt-5.5,openai-codex/gpt-5.4")
    monkeypatch.delenv("TETHER_PI_BLOCKED_MODELS", raising=False)
    monkeypatch.delenv("TETHER_PI_MODEL_BLACKLIST", raising=False)
    path = tmp_path / ".pi" / "agent" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "defaultProvider": "openai",
                "defaultModel": "gpt-6.1-sol",
                "enabledModels": ["openai/gpt-5.5", "openai/gpt-6.1-sol"],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_pi_default_overrides_legacy_environment(pi_settings: Path) -> None:
    """New Pi sessions use the saved provider and model, not stale overrides."""
    assert Settings.adapter_default_model("pi_rpc") == "openai/gpt-6.1-sol"


def test_pi_scope_overrides_legacy_environment(pi_settings: Path) -> None:
    """The picker and validator share Pi's saved scoped choices."""
    assert Settings.adapter_models("pi") == ["openai/gpt-6.1-sol", "openai/gpt-5.5"]
    assert Settings.normalize_adapter_model("pi", "gpt-6.1-sol") == "openai/gpt-6.1-sol"


def test_pi_settings_reload_without_restart(pi_settings: Path) -> None:
    """Changing Pi's saved selection updates defaults and choices on the next read."""
    Settings.adapter_models("pi")
    pi_settings.write_text(
        json.dumps(
            {
                "defaultProvider": "other",
                "defaultModel": "new-model",
                "enabledModels": ["other/new-model"],
            }
        ),
        encoding="utf-8",
    )
    assert Settings.adapter_default_model("pi") == "other/new-model"
    assert Settings.adapter_models("pi") == ["other/new-model"]


def test_custom_pi_agent_directory(
    pi_settings: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Tether reads the same custom agent directory as the Pi subprocess."""
    custom_dir = tmp_path / "custom-agent"
    custom_dir.mkdir()
    pi_settings.rename(custom_dir / "settings.json")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", "~/custom-agent")
    assert Settings.adapter_default_model("pi") == "openai/gpt-6.1-sol"
    assert Settings.adapter_models("pi") == ["openai/gpt-6.1-sol", "openai/gpt-5.5"]


@pytest.mark.parametrize("content", [None, "{", "[]", "{}"])
def test_legacy_fallback_without_pi_model_settings(
    pi_settings: Path, content: str | None
) -> None:
    """Legacy configuration still works when Pi settings are absent or unusable."""
    if content is None:
        pi_settings.unlink()
    else:
        pi_settings.write_text(content, encoding="utf-8")
    assert Settings.adapter_default_model("pi") == "openai-codex/gpt-6-astra"
    assert Settings.adapter_models("pi") == [
        "openai-codex/gpt-6-astra",
        "openai-codex/gpt-5.5",
        "openai-codex/gpt-5.4",
    ]


def test_pi_blocklist_still_applies(
    pi_settings: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pi preferences do not bypass Tether's explicit model denylist."""
    monkeypatch.setenv("TETHER_PI_BLOCKED_MODELS", "*/gpt-6.1-sol")
    assert Settings.adapter_default_model("pi") == ""
    assert Settings.adapter_models("pi") == ["openai/gpt-5.5"]


@pytest.mark.anyio
async def test_pi_api_uses_saved_scope_and_default(
    pi_settings: Path,
    api_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discovery, creation, and switching all use Pi settings despite stale env vars."""
    model_response = await api_client.get("/api/models", params={"adapter": "pi"})
    assert model_response.status_code == 200
    assert model_response.json()["default_model"] == "openai/gpt-6.1-sol"
    assert model_response.json()["available_models"] == [
        "openai/gpt-6.1-sol",
        "openai/gpt-5.5",
    ]

    created = await api_client.post("/api/sessions", json={"repo_id": "test_repo"})
    assert created.status_code == 201
    assert created.json()["model"] == "openai/gpt-6.1-sol"
    session_id = created.json()["id"]
    runner = AsyncMock()
    monkeypatch.setattr("tether.api.sessions.get_api_runner", lambda _adapter: runner)

    switched = await api_client.patch(
        f"/api/sessions/{session_id}/model", json={"model": "openai/gpt-5.5"}
    )
    assert switched.status_code == 200
    restored = await api_client.patch(
        f"/api/sessions/{session_id}/model", json={"model": "gpt-6.1-sol"}
    )
    assert restored.status_code == 200
    assert restored.json()["model"] == "openai/gpt-6.1-sol"

    typo = await api_client.patch(
        f"/api/sessions/{session_id}/model", json={"model": "openai/gpt-6-1-sol"}
    )
    assert typo.status_code == 422
    assert typo.json()["error"]["code"] == "MODEL_NOT_AVAILABLE"
