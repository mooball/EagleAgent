"""add chat_ui current-thread anchor

Revision ID: a7f3c9d2e1b4
Revises: z1a2b3c4d5e6
Create Date: 2026-09-06 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7f3c9d2e1b4'
down_revision: Union[str, Sequence[str], None] = 'z1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """One 'current thread' per user — the non-RFQ anchor for the beta chat UI."""
    op.create_table(
        'chat_ui_current_threads',
        sa.Column('user_email', sa.String(), primary_key=True),
        sa.Column('thread_id', sa.String(), nullable=False),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('NOW()'),
        ),
    )


def downgrade() -> None:
    op.drop_table('chat_ui_current_threads')
