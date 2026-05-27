"""add alt_names and alt_domains to suppliers

Revision ID: 4ca29adca5b6
Revises: a1c4e7f83b21
Create Date: 2026-05-27 15:05:46.327132

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4ca29adca5b6'
down_revision: Union[str, Sequence[str], None] = 'a1c4e7f83b21'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('suppliers', sa.Column('alt_names', sa.dialects.postgresql.JSONB(), nullable=True))
    op.add_column('suppliers', sa.Column('alt_domains', sa.dialects.postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column('suppliers', 'alt_domains')
    op.drop_column('suppliers', 'alt_names')
