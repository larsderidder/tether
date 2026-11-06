"""add bridge output policy

Revision ID: 4b7c1d2e3f40
Revises: 8c0e671ad2b8
Create Date: 2026-07-20 12:50:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "4b7c1d2e3f40"
down_revision: Union[str, Sequence[str], None] = "8c0e671ad2b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("sessions", sa.Column("bridge_verbosity", sa.String(), nullable=True))
    op.add_column(
        "sessions", sa.Column("bridge_buffer_max_seconds", sa.Float(), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_column("bridge_buffer_max_seconds")
        batch_op.drop_column("bridge_verbosity")
