"""add opportunity_sync_state to rfqs

Revision ID: e6f7a8b9c0d1
Revises: a7f3c9d2e1b4
Create Date: 2026-09-09 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e6f7a8b9c0d1'
down_revision: Union[str, Sequence[str], None] = 'a7f3c9d2e1b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Track which RFQ items have been pushed to the NetSuite opportunity."""
    op.add_column(
        'rfqs',
        sa.Column(
            'opportunity_sync_state',
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column('rfqs', 'opportunity_sync_state')
