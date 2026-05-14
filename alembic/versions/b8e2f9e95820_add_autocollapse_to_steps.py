"""add_autocollapse_to_steps

Revision ID: b8e2f9e95820
Revises: 9f2633750a73
Create Date: 2026-05-14 13:47:25.909558

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8e2f9e95820'
down_revision: Union[str, Sequence[str], None] = '9f2633750a73'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add autoCollapse column required by Chainlit >= 2.10.0."""
    op.add_column(
        'steps',
        sa.Column('autoCollapse', sa.Boolean(), server_default=sa.text('false')),
    )


def downgrade() -> None:
    """Remove autoCollapse column."""
    op.drop_column('steps', 'autoCollapse')
