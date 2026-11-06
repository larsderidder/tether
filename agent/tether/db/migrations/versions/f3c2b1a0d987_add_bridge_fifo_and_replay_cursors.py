"""Add bridge FIFO sequence and replay cursors.

Revision ID: f3c2b1a0d987
Revises: d4b6a8c2f901
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "f3c2b1a0d987"
down_revision: Union[str, Sequence[str], None] = "d4b6a8c2f901"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Store durable bridge order and per-binding replay positions."""
    op.add_column(
        "bridge_deliveries", sa.Column("delivery_seq", sa.Integer(), nullable=True)
    )

    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT id, session_id, platform FROM bridge_deliveries "
            "ORDER BY session_id, platform, "
            "CASE WHEN event_seq IS NULL THEN 1 ELSE 0 END, event_seq, created_at, id"
        )
    ).fetchall()
    counters: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (row.session_id, row.platform)
        counters[key] = counters.get(key, 0) + 1
        connection.execute(
            sa.text("UPDATE bridge_deliveries SET delivery_seq = :seq WHERE id = :id"),
            {"seq": counters[key], "id": row.id},
        )

    op.create_index(
        "ix_bridge_deliveries_delivery_seq", "bridge_deliveries", ["delivery_seq"]
    )
    op.create_table(
        "bridge_replay_cursors",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("platform", sa.String(), nullable=False),
        sa.Column("baseline_seq", sa.Integer(), nullable=False),
        sa.Column("cursor_seq", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id", "platform", name="uq_bridge_replay_cursors_binding"
        ),
    )
    op.create_index(
        "ix_bridge_replay_cursors_session_id", "bridge_replay_cursors", ["session_id"]
    )
    op.create_index(
        "ix_bridge_replay_cursors_platform", "bridge_replay_cursors", ["platform"]
    )


def downgrade() -> None:
    """Remove bridge FIFO sequence and replay positions."""
    op.drop_index(
        "ix_bridge_replay_cursors_platform", table_name="bridge_replay_cursors"
    )
    op.drop_index(
        "ix_bridge_replay_cursors_session_id", table_name="bridge_replay_cursors"
    )
    op.drop_table("bridge_replay_cursors")
    op.drop_index("ix_bridge_deliveries_delivery_seq", table_name="bridge_deliveries")
    op.drop_column("bridge_deliveries", "delivery_seq")
