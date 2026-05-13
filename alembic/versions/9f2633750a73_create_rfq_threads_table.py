"""create_rfq_threads_table

Revision ID: 9f2633750a73
Revises: ce6f6d8bc11f
Create Date: 2026-05-13 11:41:11.017478

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9f2633750a73'
down_revision: Union[str, Sequence[str], None] = 'ce6f6d8bc11f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create rfq_threads junction table for per-user RFQ↔thread binding."""
    op.create_table(
        'rfq_threads',
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('rfq_number', sa.String(), nullable=False, index=True),
        sa.Column('user_email', sa.String(), nullable=False),
        sa.Column('thread_id', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()')),
        sa.UniqueConstraint('rfq_number', 'user_email', name='uq_rfq_thread_user'),
    )


def downgrade() -> None:
    """Drop rfq_threads table."""
    op.drop_table('rfq_threads')
