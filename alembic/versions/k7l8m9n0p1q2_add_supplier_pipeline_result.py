"""add supplier_pipeline_result to email_tracking

Revision ID: k7l8m9n0p1q2
Revises: j6k7l8m9n0p1
Create Date: 2026-07-08 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = 'k7l8m9n0p1q2'
down_revision: Union[str, None] = 'ccb90336b3e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('email_tracking', sa.Column('supplier_pipeline_result', JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column('email_tracking', 'supplier_pipeline_result')
