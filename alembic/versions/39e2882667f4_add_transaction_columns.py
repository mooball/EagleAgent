"""add_transaction_columns

Revision ID: 39e2882667f4
Revises: baf9dd619f50
Create Date: 2026-05-07 16:44:44.521916

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '39e2882667f4'
down_revision: Union[str, Sequence[str], None] = 'baf9dd619f50'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('product_suppliers', sa.Column('doc_type', sa.String(), nullable=True))
    op.add_column('product_suppliers', sa.Column('netsuite_id', sa.String(), nullable=True))
    op.add_column('product_suppliers', sa.Column('cost', sa.Float(), nullable=True))
    op.add_column('product_suppliers', sa.Column('cost_currency', sa.String(length=3), nullable=True))
    op.add_column('product_suppliers', sa.Column('netsuite_last_modified', sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f('ix_product_suppliers_doc_type'), 'product_suppliers', ['doc_type'], unique=False)
    op.create_unique_constraint('uq_product_suppliers_netsuite_id', 'product_suppliers', ['netsuite_id'])

    # Backfill existing rows as PurchaseOrder
    op.execute("UPDATE product_suppliers SET doc_type = 'PurchaseOrder' WHERE doc_type IS NULL")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('uq_product_suppliers_netsuite_id', 'product_suppliers', type_='unique')
    op.drop_index(op.f('ix_product_suppliers_doc_type'), table_name='product_suppliers')
    op.drop_column('product_suppliers', 'netsuite_last_modified')
    op.drop_column('product_suppliers', 'cost_currency')
    op.drop_column('product_suppliers', 'cost')
    op.drop_column('product_suppliers', 'netsuite_id')
    op.drop_column('product_suppliers', 'doc_type')
