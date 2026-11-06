"""add_external_agent_fields

Revision ID: e57f25d5ec90
Revises: b302da14a242
Create Date: 2026-02-03 23:15:58.015691

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e57f25d5ec90'
down_revision: Union[str, Sequence[str], None] = 'b302da14a242'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add external agent metadata fields
    op.add_column('sessions', sa.Column('external_agent_id', sa.String(), nullable=True))
    op.add_column('sessions', sa.Column('external_agent_name', sa.String(), nullable=True))
    op.add_column('sessions', sa.Column('external_agent_type', sa.String(), nullable=True))
    op.add_column('sessions', sa.Column('external_agent_icon', sa.String(), nullable=True))
    op.add_column('sessions', sa.Column('external_agent_workspace', sa.String(), nullable=True))

    # Add platform binding fields
    op.add_column('sessions', sa.Column('platform', sa.String(), nullable=True))
    op.add_column('sessions', sa.Column('platform_thread_id', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('sessions') as batch_op:
        batch_op.drop_column('external_agent_id')
        batch_op.drop_column('external_agent_name')
        batch_op.drop_column('external_agent_type')
        batch_op.drop_column('external_agent_icon')
        batch_op.drop_column('external_agent_workspace')
        batch_op.drop_column('platform')
        batch_op.drop_column('platform_thread_id')
