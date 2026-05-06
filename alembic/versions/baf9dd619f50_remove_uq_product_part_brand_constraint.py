"""remove uq_product_part_brand constraint

Revision ID: baf9dd619f50
Revises: 4290abdfe68e
Create Date: 2026-05-06 17:32:23.580624

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'baf9dd619f50'
down_revision: Union[str, Sequence[str], None] = '4290abdfe68e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint('uq_product_part_brand', 'products', type_='unique')
    op.create_unique_constraint('uq_product_netsuite_id', 'products', ['netsuite_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('uq_product_netsuite_id', 'products', type_='unique')
    op.create_unique_constraint('uq_product_part_brand', 'products', ['part_number', 'brand'])
