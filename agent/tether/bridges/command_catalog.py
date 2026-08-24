"""
Shared command catalog for bridge help text and command menus.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BridgeCommand:
    """
    Describe one user-facing bridge command.
    """

    name: str
    args: str
    description: str
    platforms: frozenset[str]
    scope: str = "session"
    menu: bool = True

    def render(self, prefix: str) -> str:
        """
        Render the command for text bridge help.
        """
        suffix = f" {self.args}" if self.args else ""
        return f"{prefix}{self.name}{suffix} - {self.description}"


COMMANDS: tuple[BridgeCommand, ...] = (
    BridgeCommand(
        "status",
        "",
        "List all sessions",
        frozenset({"telegram", "slack", "discord"}),
        "global",
    ),
    BridgeCommand(
        "list",
        "[page|search]",
        "List external sessions (Claude Code, Codex)",
        frozenset({"telegram", "slack", "discord"}),
        "global",
    ),
    BridgeCommand(
        "attach",
        "<number> [force]",
        "Attach to an external session",
        frozenset({"telegram", "slack", "discord"}),
        "global",
    ),
    BridgeCommand(
        "new",
        "[agent] [directory]",
        "Start a new session",
        frozenset({"telegram", "slack", "discord"}),
        "global",
    ),
    BridgeCommand(
        "new",
        "--clone <url> [-b branch] [-a adapter] [-m prompt]",
        "Clone and start",
        frozenset({"telegram", "slack", "discord"}),
        "global",
        False,
    ),
    BridgeCommand(
        "new",
        "--template <name> [-m prompt]",
        "Start from template",
        frozenset({"telegram", "slack", "discord"}),
        "global",
        False,
    ),
    BridgeCommand(
        "stop",
        "",
        "Interrupt the session in this thread",
        frozenset({"slack", "discord"}),
    ),
    BridgeCommand(
        "stop", "", "Interrupt the session in this topic", frozenset({"telegram"})
    ),
    BridgeCommand(
        "sync",
        "",
        "Pull new messages from the attached external session",
        frozenset({"telegram", "slack", "discord"}),
    ),
    BridgeCommand(
        "usage",
        "",
        "Show token usage and cost for this session",
        frozenset({"telegram", "slack", "discord"}),
    ),
    BridgeCommand(
        "compact",
        "[instructions]",
        "Compact pi context for this session",
        frozenset({"telegram", "slack", "discord"}),
    ),
    BridgeCommand(
        "models",
        "",
        "List configured models for this session",
        frozenset({"telegram", "slack", "discord"}),
    ),
    BridgeCommand(
        "model",
        "[model]",
        "Show or switch this session's model",
        frozenset({"telegram", "slack", "discord"}),
    ),
    BridgeCommand(
        "verbosity",
        "[none|minimal|medium|high]",
        "Show or set bridge output verbosity",
        frozenset({"telegram", "slack", "discord"}),
    ),
    BridgeCommand(
        "buffer",
        "[seconds|off]",
        "Show or set bridge output buffering",
        frozenset({"telegram", "slack", "discord"}),
    ),
    BridgeCommand(
        "rename",
        "<name>",
        "Rename this Discord thread",
        frozenset({"discord"}),
        "session",
        False,
    ),
    BridgeCommand(
        "setup",
        "<code>",
        "Configure this channel as the control channel and pair you",
        frozenset({"discord"}),
        "global",
        False,
    ),
    BridgeCommand(
        "pair",
        "<code>",
        "Pair your Discord user to authorize commands",
        frozenset({"discord"}),
        "global",
        False,
    ),
    BridgeCommand(
        "pair-status",
        "",
        "Show whether you are authorized",
        frozenset({"discord"}),
        "global",
        False,
    ),
    BridgeCommand(
        "help",
        "",
        "Show this help",
        frozenset({"telegram", "slack", "discord"}),
        "global",
    ),
    BridgeCommand(
        "git",
        "",
        "Show git status (branch, changes, last commit)",
        frozenset({"telegram", "slack", "discord"}),
        "git",
    ),
    BridgeCommand(
        "diff",
        "",
        "Show what changed (summary and patch file)",
        frozenset({"telegram"}),
        "git",
    ),
    BridgeCommand("log", "[n]", "Show recent commits", frozenset({"telegram"}), "git"),
    BridgeCommand(
        "pr",
        "<title> [--draft]",
        "Create a pull or merge request",
        frozenset({"telegram", "slack", "discord"}),
        "git",
    ),
)


def commands_for(platform: str, *, menu_only: bool = False) -> list[BridgeCommand]:
    """
    Return commands visible on a bridge platform.
    """
    return [
        command
        for command in COMMANDS
        if platform in command.platforms and (command.menu or not menu_only)
    ]


def telegram_menu_commands() -> list[tuple[str, str]]:
    """
    Return Telegram BotCommand input as command and description pairs.
    """
    seen: set[str] = set()
    items: list[tuple[str, str]] = []
    for command in commands_for("telegram", menu_only=True):
        if command.name in seen:
            continue
        seen.add(command.name)
        items.append((command.name, command.description))
    return items


def help_text(platform: str, *, prefix: str) -> str:
    """
    Build help text for one text bridge platform.
    """
    general = [command for command in commands_for(platform) if command.scope != "git"]
    git = [command for command in commands_for(platform) if command.scope == "git"]
    thread_word = "topic" if platform == "telegram" else "thread"

    lines = ["Tether Commands:", ""]
    lines.extend(command.render(prefix) for command in general)
    if git:
        lines.extend(["", f"Git Commands (inside a session {thread_word}):"])
        lines.extend(command.render(prefix) for command in git)
    lines.extend(
        ["", f"Send a text message in a session {thread_word} to forward it as input."]
    )
    return "\n".join(lines)
