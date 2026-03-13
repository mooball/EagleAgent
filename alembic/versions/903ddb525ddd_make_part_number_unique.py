"""Make part_number unique

Revision ID: 903ddb525ddd
Revises: d5487949a63a
Create Date: 2026-03-13 13:46:08.400928

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '903ddb525ddd'
down_revision: Union[str, Sequence[str], None] = 'd5487949a63a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop the non-unique index and create a unique one or add a unique constraint
    op.drop_index(op.f('ix_products_part_number'), table_name='products')
    op.create_index(op.f('ix_products_part_number'), 'products', ['part_number'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_products_part_number'), table_name='products')
    op.create_index(op.f('ix_products_part_number'), 'products', ['part_number'], unique=False)
