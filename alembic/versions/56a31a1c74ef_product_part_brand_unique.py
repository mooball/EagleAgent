"""product_part_brand_unique

Revision ID: 56a31a1c74ef
Revises: 7051fd4a9d34
Create Date: 2026-03-23 13:37:39.481133

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '56a31a1c74ef'
down_revision: Union[str, Sequence[str], None] = '7051fd4a9d34'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint('products_netsuite_id_key', 'products', type_='unique')
    op.drop_index('ix_products_part_number', table_name='products')
    op.create_index('ix_products_part_number', 'products', ['part_number'], unique=False)
    op.create_unique_constraint('uq_product_part_brand', 'products', ['part_number', 'brand'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('uq_product_part_brand', 'products', type_='unique')
    op.drop_index('ix_products_part_number', table_name='products')
    op.create_index('ix_products_part_number', 'products', ['part_number'], unique=True)
    op.create_unique_constraint('products_netsuite_id_key', 'products', ['netsuite_id'])
