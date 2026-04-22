"""Add pg_trgm extension

Revision ID: ddbd16aabcbf
Revises: ad0d83d7b96f
Create Date: 2026-04-22 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'ddbd16aabcbf'
down_revision: Union[str, Sequence[str], None] = 'ad0d83d7b96f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS pg_trgm;")
