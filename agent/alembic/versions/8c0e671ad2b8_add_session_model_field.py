"""add session model field

Revision ID: 8c0e671ad2b8
Revises: f6a2c9d04b31
Create Date: 2026-06-14 17:35:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "8c0e671ad2b8"
down_revision: Union[str, Sequence[str], None] = "f6a2c9d04b31"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("sessions", sa.Column("model", sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_column("model")
