"""add netsuite_last_modified to products

Revision ID: 4290abdfe68e
Revises: 5b0642094032
Create Date: 2026-05-06 17:18:53.150728

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4290abdfe68e'
down_revision: Union[str, Sequence[str], None] = '5b0642094032'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('products', sa.Column('netsuite_last_modified', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('products', 'netsuite_last_modified')
