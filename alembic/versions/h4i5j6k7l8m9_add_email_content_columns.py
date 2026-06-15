"""add email content columns for Phase 6.2

Revision ID: h4i5j6k7l8m9
Revises: g3h4i5j6k7l8
Create Date: 2026-06-15 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'h4i5j6k7l8m9'
down_revision: Union[str, Sequence[str], None] = 'g3h4i5j6k7l8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('email_tracking', sa.Column('body_markdown', sa.Text(), nullable=True))
    op.add_column('email_tracking', sa.Column('body_html', sa.Text(), nullable=True))
    op.add_column('email_tracking', sa.Column('attachments_json', sa.JSON(), nullable=True))
    op.add_column('email_tracking', sa.Column('sender_name', sa.String(), nullable=True))
    op.add_column('email_tracking', sa.Column('all_recipients', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('email_tracking', 'all_recipients')
    op.drop_column('email_tracking', 'sender_name')
    op.drop_column('email_tracking', 'attachments_json')
    op.drop_column('email_tracking', 'body_html')
    op.drop_column('email_tracking', 'body_markdown')
