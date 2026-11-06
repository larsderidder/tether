"""Add durable bridge delivery outbox.

Revision ID: d4b6a8c2f901
Revises: a61c0b7d2e45
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4b6a8c2f901"
down_revision: Union[str, Sequence[str], None] = "a61c0b7d2e45"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the bridge delivery outbox table."""
    op.create_table(
        "bridge_deliveries",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("platform", sa.String(), nullable=False),
        sa.Column("delivery_key", sa.String(), nullable=False),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("payload", sa.String(), nullable=False),
        sa.Column("event_seq", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.String(), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("sent_at", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("delivery_key"),
    )
    op.create_index(
        "ix_bridge_deliveries_session_id", "bridge_deliveries", ["session_id"]
    )
    op.create_index("ix_bridge_deliveries_platform", "bridge_deliveries", ["platform"])
    op.create_index(
        "ix_bridge_deliveries_delivery_key", "bridge_deliveries", ["delivery_key"]
    )
    op.create_index(
        "ix_bridge_deliveries_event_seq", "bridge_deliveries", ["event_seq"]
    )
    op.create_index("ix_bridge_deliveries_status", "bridge_deliveries", ["status"])


def downgrade() -> None:
    """Drop the bridge delivery outbox table."""
    op.drop_index("ix_bridge_deliveries_status", table_name="bridge_deliveries")
    op.drop_index("ix_bridge_deliveries_event_seq", table_name="bridge_deliveries")
    op.drop_index("ix_bridge_deliveries_delivery_key", table_name="bridge_deliveries")
    op.drop_index("ix_bridge_deliveries_platform", table_name="bridge_deliveries")
    op.drop_index("ix_bridge_deliveries_session_id", table_name="bridge_deliveries")
    op.drop_table("bridge_deliveries")
