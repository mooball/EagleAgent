"""add command and modes columns to steps

Revision ID: ad0d83d7b96f
Revises: 02032d8c56a9
Create Date: 2026-04-12 12:36:02.487257

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'ad0d83d7b96f'
down_revision: Union[str, Sequence[str], None] = '02032d8c56a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add command and modes columns to steps table for Chainlit command support."""
    op.add_column('steps', sa.Column('command', sa.Text(), nullable=True))
    op.add_column('steps', sa.Column('modes', sa.JSON(), nullable=True))


def downgrade() -> None:
    """Remove command and modes columns from steps table."""
    op.drop_column('steps', 'modes')
    op.drop_column('steps', 'command')
