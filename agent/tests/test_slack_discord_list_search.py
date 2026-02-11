from __future__ import annotations

from tether.bridges.slack.bot import SlackBridge
from tether.bridges.discord.bot import DiscordBridge


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


def test_slack_list_search_filters_and_numbers_match_view() -> None:
    b = SlackBridge(bot_token="x", channel_id="c")
    b._cached_external = _mk_sessions(25)  # noqa: SLF001 (test)
    b._set_external_view("repo-1")  # noqa: SLF001 (test)
    text, page, total_pages = b._format_external_page(1)  # noqa: SLF001 (test)

    assert page == 1
    assert total_pages == 2
    assert "[search: repo-1]" in text
    # "repo-1" and "repo-10" etc can match, but numbering must be 1..N for the view.
    assert "1. `repo-1`" in text


def test_discord_list_page_2_starts_at_11() -> None:
    b = DiscordBridge(bot_token="x", channel_id=123)
    b._cached_external = _mk_sessions(25)  # noqa: SLF001 (test)
    b._set_external_view(None)  # noqa: SLF001 (test)
    text, page, total_pages = b._format_external_page(2)  # noqa: SLF001 (test)

    assert page == 2
    assert total_pages == 3
    assert "11. `repo-10`" in text
    assert "20. `repo-19`" in text
    assert "21. " not in text
