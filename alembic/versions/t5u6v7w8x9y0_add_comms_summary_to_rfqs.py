"""add comms_summary to rfqs

Revision ID: t5u6v7w8x9y0
Revises: s4t5u6v7w8x9
Create Date: 2026-08-14 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = 't5u6v7w8x9y0'
down_revision: Union[str, None] = 's4t5u6v7w8x9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('rfqs', sa.Column('comms_summary', JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column('rfqs', 'comms_summary')
