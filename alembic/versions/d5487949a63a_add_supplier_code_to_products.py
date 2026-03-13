"""Add supplier_code to products

Revision ID: d5487949a63a
Revises: 16e5f89ec7ba
Create Date: 2026-03-13 13:32:05.358233

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd5487949a63a'
down_revision: Union[str, Sequence[str], None] = '16e5f89ec7ba'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    op.add_column('products', sa.Column('supplier_code', sa.String(), nullable=True))

def downgrade() -> None:
    op.drop_column('products', 'supplier_code')
