# Code Standards

This document defines the minimum code style expectations for Codex on Mobile.

## Scope
- **Python (agent)**: Standards below are required.
- **Other languages**: Follow existing project conventions; add language-specific standards later as needed.

## Formatting (Python)
- Use **Black** with the project configuration in `agent/pyproject.toml`.
- Do not hand-format Python; run Black to normalize layout.
- Default command:
  - `cd agent && python -m black .`

## Docstrings
- Use module, class, and function docstrings for any non-trivial logic.
- Keep docstrings concise and factual; avoid restating obvious code.
- Use **Args/Returns/Raises** sections when parameters or behavior are not obvious.
- Skip argument docs for simple helpers or where names/types are self-evident.

Example style:
```python
def fetch(session_id: str, timeout_s: float) -> str:
    """Fetch a session payload from the store.

    Args:
        session_id: Internal session identifier.
        timeout_s: Timeout in seconds.
    """
```

## Inline Comments
- Add comments to explain **why** or **non-obvious behavior**.
- Avoid comments for trivial control flow or obvious assignments.
- Prefer short comments adjacent to the relevant logic.

## Type Hints
- Keep type annotations up to date when adding or modifying functions.
- Use existing patterns in the file; avoid introducing complex generics without need.
- Prefer modern built-in generics (`list`, `dict`, `tuple`, `set`) and `| None` over `typing.List`/`typing.Dict`/`typing.Optional`.

## Logging
- Use structured logging (structlog) in API and runner code paths.
- Bind a request identifier (and other useful context) for HTTP request logs.
- Include key identifiers (e.g., `session_id`) when logging session-related work.

## Git Commit Messages
- Use a single-line commit message (no multi-line descriptions).
- Keep messages concise and descriptive of the change.
- Use lowercase for the start of the message (e.g., "Add feature" not "add feature").
- Do not include AI attribution (no "Co-Authored-By" lines).

Examples:
```
Add settings module tests
Refactor sidecar into modular structure with centralized settings
Fix token validation in auth middleware
```

## Changes to These Standards
- Update this document when conventions change or new languages are added.
