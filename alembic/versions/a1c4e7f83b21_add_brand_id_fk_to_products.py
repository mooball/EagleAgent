"""add brand_id FK to products

Revision ID: a1c4e7f83b21
Revises: 3a80d695b72e
Create Date: 2026-05-27

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c4e7f83b21'
down_revision: Union[str, Sequence[str], None] = '3a80d695b72e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add brand_id FK to products table and backfill from brand name."""
    op.add_column('products', sa.Column('brand_id', sa.UUID(), nullable=True))
    op.create_index(op.f('ix_products_brand_id'), 'products', ['brand_id'], unique=False)
    op.create_foreign_key('fk_products_brand_id', 'products', 'brands', ['brand_id'], ['id'])

    # Backfill brand_id from existing brand name string
    op.execute("""
        UPDATE products p
        SET brand_id = b.id
        FROM brands b
        WHERE LOWER(p.brand) = LOWER(b.name)
          AND b.duplicate_of IS NULL
          AND p.brand IS NOT NULL
          AND p.brand_id IS NULL
    """)


def downgrade() -> None:
    """Remove brand_id FK from products."""
    op.drop_constraint('fk_products_brand_id', 'products', type_='foreignkey')
    op.drop_index(op.f('ix_products_brand_id'), table_name='products')
    op.drop_column('products', 'brand_id')
