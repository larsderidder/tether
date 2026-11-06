"""add session working_branch field

Revision ID: c4f1a2b3d8e9
Revises: 25f0e9e77fed
Create Date: 2026-02-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c4f1a2b3d8e9"
down_revision: Union[str, Sequence[str], None] = "25f0e9e77fed"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("sessions", sa.Column("working_branch", sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_column("working_branch")
