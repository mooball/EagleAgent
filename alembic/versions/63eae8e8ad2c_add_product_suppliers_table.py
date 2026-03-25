"""add_product_suppliers_table

Revision ID: 63eae8e8ad2c
Revises: 56a31a1c74ef
Create Date: 2026-03-25 13:41:45.678901

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '63eae8e8ad2c'
down_revision: Union[str, Sequence[str], None] = '56a31a1c74ef'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('product_suppliers',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('po_number', sa.String(), nullable=False),
    sa.Column('date', sa.Date(), nullable=True),
    sa.Column('product_id', sa.UUID(), nullable=False),
    sa.Column('supplier_id', sa.UUID(), nullable=False),
    sa.Column('quantity', sa.Float(), nullable=True),
    sa.Column('price', sa.Float(), nullable=True),
    sa.Column('status', sa.String(), nullable=True),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], ),
    sa.ForeignKeyConstraint(['supplier_id'], ['suppliers.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_product_suppliers_po_number'), 'product_suppliers', ['po_number'], unique=False)
    op.create_index(op.f('ix_product_suppliers_product_id'), 'product_suppliers', ['product_id'], unique=False)
    op.create_index(op.f('ix_product_suppliers_supplier_id'), 'product_suppliers', ['supplier_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_product_suppliers_supplier_id'), table_name='product_suppliers')
    op.drop_index(op.f('ix_product_suppliers_product_id'), table_name='product_suppliers')
    op.drop_index(op.f('ix_product_suppliers_po_number'), table_name='product_suppliers')
    op.drop_table('product_suppliers')
