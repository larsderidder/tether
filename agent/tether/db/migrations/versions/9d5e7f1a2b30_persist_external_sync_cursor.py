"""persist external sync cursor

Revision ID: 9d5e7f1a2b30
Revises: 4b7c1d2e3f40
Create Date: 2026-07-28 11:46:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "9d5e7f1a2b30"
down_revision: Union[str, Sequence[str], None] = "4b7c1d2e3f40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Persist external transcript synchronization counters."""
    op.add_column(
        "sessions",
        sa.Column("synced_message_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column("synced_turn_count", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    """Remove persisted external transcript synchronization counters."""
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_column("synced_turn_count")
        batch_op.drop_column("synced_message_count")
