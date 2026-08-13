"""add feedback to email_tracking

Revision ID: s4t5u6v7w8x9
Revises: r3s4t5u6v7w8
Create Date: 2026-08-13 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = 's4t5u6v7w8x9'
down_revision: Union[str, None] = 'r3s4t5u6v7w8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('email_tracking', sa.Column('feedback', JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column('email_tracking', 'feedback')
