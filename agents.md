# Agent Guidelines

This document provides essential context for AI agents working on this codebase.

For detailed background information, see the `background/` directory.

## Project Overview

Tether is a **local-first control plane for supervising AI work**. It lets you start, monitor, and guide AI-driven tasks from anywhere—especially your phone—without giving up control, visibility, or ownership of your environment.

Key principles:
- **Local-first**: Runs on your machine, no cloud dependency
- **Human-in-the-loop**: AI is not autonomous; human remains in control
- **Observable over magical**: Visible logs, explicit diffs, simple primitives

## Git Commit Policy

**This is critical. Follow these rules exactly:**

- Use a **single-line commit message** (no multi-line descriptions)
- Keep messages concise and descriptive
- Use **sentence case** (e.g., "Add feature" not "add feature")
- **Do not include AI attribution** (no "Co-Authored-By" lines)

Examples:
```
Add settings module tests
Refactor sidecar into modular structure with centralized settings
Fix token validation in auth middleware
```

## Code Standards

- **Python**: Use Black formatter (`cd agent && python -m black .`)
- **Type hints**: Keep annotations up to date; prefer modern syntax (`list`, `dict`, `| None`)
- **Logging**: Use structured logging (structlog) with request identifiers
- **Docstrings**: Use for non-trivial logic; keep concise

See `background/CODE_STANDARDS.md` for full details.

## Architecture

Components:
- **Agent (Python/FastAPI)**: HTTP API, SSE streaming, static UI hosting
- **UI (Vue 3)**: Mobile-first interface for session monitoring
- **Runner**: Execution adapter (Codex CLI, Claude, etc.)

See `background/ARCHITECTURE.md` for details.

## Key Background Documents

| Document | Purpose |
|----------|---------|
| `background/GOAL.md` | Project philosophy and success criteria |
| `background/ARCHITECTURE.md` | System components and data flow |
| `background/PROTOCOL.md` | HTTP API and SSE protocol specification |
| `background/RUNNER_SPEC.md` | Runner contract and event semantics |
| `background/CODE_STANDARDS.md` | Formatting, typing, logging standards |
| `background/ROADMAP.md` | Development phases and priorities |

## Design Principles

- Local-first
- Explicit > implicit
- Observable > automated
- Simple > clever
- Human remains in control
