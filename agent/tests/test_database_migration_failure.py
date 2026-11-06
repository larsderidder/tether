"""Schema migration errors must not be hidden by table creation."""

import pytest

from tether.db import init_db


def test_migration_failure_prevents_opening_incompatible_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed upgrade must surface before runners or bridge delivery start."""

    def failed_upgrade() -> None:
        """Emulate an incompatible or partially migrated database."""
        raise RuntimeError("schema upgrade failed")

    monkeypatch.setattr("tether.db._run_migrations", failed_upgrade)
    with pytest.raises(RuntimeError, match="schema upgrade failed"):
        init_db()
