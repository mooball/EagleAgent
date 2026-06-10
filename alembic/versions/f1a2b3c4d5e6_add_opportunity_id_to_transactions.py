"""Add opportunity_id to transactions (product_suppliers).

Revision ID: f1a2b3c4d5e6
Revises: e8f2a1b7c4d3
Create Date: 2025-06-12 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'f1a2b3c4d5e6'
down_revision = 'e8f2a1b7c4d3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('product_suppliers', sa.Column('netsuite_opportunity_id', sa.String(), nullable=True))
    op.add_column('product_suppliers', sa.Column('opportunity_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index('ix_product_suppliers_netsuite_opportunity_id', 'product_suppliers', ['netsuite_opportunity_id'])
    op.create_index('ix_product_suppliers_opportunity_id', 'product_suppliers', ['opportunity_id'])
    op.create_foreign_key(
        'fk_product_suppliers_opportunity_id',
        'product_suppliers',
        'opportunities',
        ['opportunity_id'],
        ['id'],
    )


def downgrade() -> None:
    op.drop_constraint('fk_product_suppliers_opportunity_id', 'product_suppliers', type_='foreignkey')
    op.drop_index('ix_product_suppliers_opportunity_id', table_name='product_suppliers')
    op.drop_index('ix_product_suppliers_netsuite_opportunity_id', table_name='product_suppliers')
    op.drop_column('product_suppliers', 'opportunity_id')
    op.drop_column('product_suppliers', 'netsuite_opportunity_id')
