"""add pipeline_stage to rfqs

Revision ID: j6k7l8m9n0p1
Revises: i5j6k7l8m9n0
Create Date: 2026-06-25 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'j6k7l8m9n0p1'
down_revision: Union[str, None] = 'i5j6k7l8m9n0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('rfqs', sa.Column('pipeline_stage', sa.String(), server_default='unprocessed', nullable=False))


def downgrade() -> None:
    op.drop_column('rfqs', 'pipeline_stage')
