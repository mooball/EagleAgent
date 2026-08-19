"""add department_id to products

Revision ID: v7w8x9y0z1a2
Revises: u6v7w8x9y0z1
Create Date: 2026-08-19 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'v7w8x9y0z1a2'
down_revision: Union[str, None] = 'u6v7w8x9y0z1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('products', sa.Column('department_id', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('products', 'department_id')
