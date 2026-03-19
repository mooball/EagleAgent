"""expand_suppliers_add_supplier_brands

Revision ID: 931fa34911ec
Revises: 6a57b57944ae
Create Date: 2026-03-19 10:26:12.537181

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '931fa34911ec'
down_revision: Union[str, Sequence[str], None] = '6a57b57944ae'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('supplier_brands',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('supplier_id', sa.UUID(), nullable=False),
    sa.Column('brand_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['brand_id'], ['brands.id'], ),
    sa.ForeignKeyConstraint(['supplier_id'], ['suppliers.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('supplier_id', 'brand_id', name='uq_supplier_brand')
    )
    op.create_index(op.f('ix_supplier_brands_brand_id'), 'supplier_brands', ['brand_id'], unique=False)
    op.create_index(op.f('ix_supplier_brands_supplier_id'), 'supplier_brands', ['supplier_id'], unique=False)
    op.add_column('suppliers', sa.Column('url', sa.String(), nullable=True))
    op.add_column('suppliers', sa.Column('address_1', sa.String(), nullable=True))
    op.add_column('suppliers', sa.Column('city', sa.String(), nullable=True))
    op.add_column('suppliers', sa.Column('country', sa.String(), nullable=True))
    op.add_column('suppliers', sa.Column('notes', sa.Text(), nullable=True))
    op.add_column('suppliers', sa.Column('contacts', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('suppliers', 'contacts')
    op.drop_column('suppliers', 'notes')
    op.drop_column('suppliers', 'country')
    op.drop_column('suppliers', 'city')
    op.drop_column('suppliers', 'address_1')
    op.drop_column('suppliers', 'url')
    op.drop_index(op.f('ix_supplier_brands_supplier_id'), table_name='supplier_brands')
    op.drop_index(op.f('ix_supplier_brands_brand_id'), table_name='supplier_brands')
    op.drop_table('supplier_brands')
