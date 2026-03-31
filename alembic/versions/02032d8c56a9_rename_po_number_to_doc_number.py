"""rename_po_number_to_doc_number

Revision ID: 02032d8c56a9
Revises: 63eae8e8ad2c
Create Date: 2026-03-31 20:11:53.709035

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '02032d8c56a9'
down_revision: Union[str, Sequence[str], None] = '63eae8e8ad2c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column('product_suppliers', 'po_number', new_column_name='doc_number')
    op.drop_index('ix_product_suppliers_po_number', table_name='product_suppliers')
    op.create_index(op.f('ix_product_suppliers_doc_number'), 'product_suppliers', ['doc_number'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_product_suppliers_doc_number'), table_name='product_suppliers')
    op.create_index('ix_product_suppliers_po_number', 'product_suppliers', ['po_number'], unique=False)
    op.alter_column('product_suppliers', 'doc_number', new_column_name='po_number')
