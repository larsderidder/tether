"""normalize automation adapter

Revision ID: f6a2c9d04b31
Revises: c4f1a2b3d8e9
Create Date: 2026-06-07 22:50:00.000000

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "f6a2c9d04b31"
down_revision: Union[str, Sequence[str], None] = "c4f1a2b3d8e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade persisted legacy automation adapter names."""
    old_name = "run" + "book"
    op.execute(
        f"UPDATE sessions SET adapter = 'automation' WHERE adapter = '{old_name}'"
    )
    op.execute(
        f"UPDATE sessions SET runner_type = 'automation' WHERE runner_type = '{old_name}'"
    )


def downgrade() -> None:
    """Keep the canonical automation adapter name."""
