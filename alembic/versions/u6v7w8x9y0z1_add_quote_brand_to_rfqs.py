"""add quote_brand to rfqs

Revision ID: u6v7w8x9y0z1
Revises: t5u6v7w8x9y0
Create Date: 2026-08-18 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision: str = 'u6v7w8x9y0z1'
down_revision: Union[str, None] = 't5u6v7w8x9y0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('rfqs', sa.Column('quote_brand_id', UUID(as_uuid=True), nullable=True))
    op.add_column('rfqs', sa.Column('quote_brand', sa.String(), nullable=True))
    op.create_index('ix_rfqs_quote_brand_id', 'rfqs', ['quote_brand_id'])


def downgrade() -> None:
    op.drop_index('ix_rfqs_quote_brand_id', table_name='rfqs')
    op.drop_column('rfqs', 'quote_brand')
    op.drop_column('rfqs', 'quote_brand_id')
