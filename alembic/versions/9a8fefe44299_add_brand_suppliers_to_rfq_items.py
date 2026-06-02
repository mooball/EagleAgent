"""add_brand_suppliers_to_rfq_items

Revision ID: 9a8fefe44299
Revises: 53679d7c1322
Create Date: 2026-06-02 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = '9a8fefe44299'
down_revision: Union[str, None] = '53679d7c1322'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('rfq_items', sa.Column('brand_suppliers', JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column('rfq_items', 'brand_suppliers')
