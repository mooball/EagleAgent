"""add_supplier_address_hubspot_netsuite_fields

Revision ID: 5b0642094032
Revises: bab214043ed3
Create Date: 2026-05-06 15:25:45.617122

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5b0642094032'
down_revision: Union[str, Sequence[str], None] = 'bab214043ed3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('suppliers', sa.Column('address_2', sa.String(), nullable=True))
    op.add_column('suppliers', sa.Column('state', sa.String(), nullable=True))
    op.add_column('suppliers', sa.Column('postcode', sa.String(), nullable=True))
    op.add_column('suppliers', sa.Column('hubspot_id', sa.String(), nullable=True))
    op.add_column('suppliers', sa.Column('netsuite_last_modified', sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f('ix_suppliers_hubspot_id'), 'suppliers', ['hubspot_id'], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_suppliers_hubspot_id'), table_name='suppliers')
    op.drop_column('suppliers', 'netsuite_last_modified')
    op.drop_column('suppliers', 'hubspot_id')
    op.drop_column('suppliers', 'postcode')
    op.drop_column('suppliers', 'state')
    op.drop_column('suppliers', 'address_2')
