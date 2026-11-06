"""Rebase legacy tail-relative transcript cursors.

Revision ID: a61c0b7d2e45
Revises: 9d5e7f1a2b30
"""

from alembic import op

revision = "a61c0b7d2e45"
down_revision = "9d5e7f1a2b30"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Establish a fresh absolute baseline without replaying an ambiguous prefix."""
    # Older cursors counted a bounded history window, not the full transcript.
    # NULL uses the existing baseline-without-replay path on the first sync.
    op.execute(
        "UPDATE sessions SET synced_message_count = NULL, synced_turn_count = NULL "
        "WHERE runner_session_id IS NOT NULL"
    )


def downgrade() -> None:
    """Retain NULL baselines; the old tail-relative indices cannot be recovered."""
