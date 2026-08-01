"""add_rfq_creation_result_to_email_tracking

Revision ID: q2r3s4t5u6v7
Revises: p1q2r3s4t5u6
Create Date: 2026-07-30 13:00:00.000000

Add rfq_creation_result JSONB column to email_tracking for the
RFQ creation pipeline's idempotency guard and result storage.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = 'q2r3s4t5u6v7'
down_revision: Union[str, Sequence[str], None] = 'p1q2r3s4t5u6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('email_tracking',
                  sa.Column('rfq_creation_result', JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column('email_tracking', 'rfq_creation_result')
