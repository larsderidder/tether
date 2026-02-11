from pathlib import Path

import pytest

from tether.bridges.telegram.bot import TelegramBridge
from tether.bridges.telegram.state import StateManager


def _mk_bridge(tmp_path: Path) -> TelegramBridge:
    state = StateManager(str(tmp_path / "telegram_state.json"))
    # Don't load from disk during tests.
    return TelegramBridge(bot_token="x", forum_group_id=123, state_manager=state)


def _mk_sessions(n: int) -> list[dict]:
    out: list[dict] = []
    for i in range(n):
        out.append(
            {
                "id": f"id-{i}",
                "runner_type": "codex" if i % 2 else "claude_code",
                "directory": f"/tmp/repo-{i}",
                "first_prompt": f"prompt-{i}",
                "last_prompt": f"prompt-{i}",
                "is_running": bool(i % 2),
            }
        )
    return out


@pytest.mark.anyio
async def test_external_page_numbers_are_global(tmp_path: Path) -> None:
    bridge = _mk_bridge(tmp_path)
    bridge._cached_external = _mk_sessions(25)  # noqa: SLF001 (test)
    bridge._set_external_view(None)  # noqa: SLF001 (test)

    text, page, total_pages = bridge._format_external_page(2)  # noqa: SLF001 (test)

    assert page == 2
    assert total_pages == 3
    # Page size is 10, so page 2 starts at item 11.
    assert "11. `repo-10`" in text
    assert "20. `repo-19`" in text
    assert "21. " not in text


@pytest.mark.anyio
async def test_external_page_clamps_out_of_range(tmp_path: Path) -> None:
    bridge = _mk_bridge(tmp_path)
    bridge._cached_external = _mk_sessions(3)  # noqa: SLF001 (test)
    bridge._set_external_view(None)  # noqa: SLF001 (test)

    text, page, total_pages = bridge._format_external_page(999)  # noqa: SLF001 (test)

    assert page == 1
    assert total_pages == 1
    assert "prompt-0" in text


@pytest.mark.anyio
async def test_external_search_filters_by_directory(tmp_path: Path) -> None:
    bridge = _mk_bridge(tmp_path)
    bridge._cached_external = _mk_sessions(12)  # noqa: SLF001 (test)

    bridge._set_external_view("repo-1")  # noqa: SLF001 (test)
    text, page, total_pages = bridge._format_external_page(1)  # noqa: SLF001 (test)

    assert page == 1
    assert total_pages == 1
    assert "[search: repo-1]" in text
    assert "repo-1" in text


@pytest.mark.anyio
async def test_make_external_topic_name_is_short(tmp_path: Path) -> None:
    bridge = _mk_bridge(tmp_path)
    name = bridge._make_external_topic_name(  # noqa: SLF001 (test)
        directory="/home/lars/some/really-really-really-long-directory-name-for-a-repo",
        session_id="sess_1234567890abcdef",
    )
    assert len(name) <= 64
    # Name should be the directory UpperCased, no ID suffix
    assert name.startswith("Really-really-really-long")
