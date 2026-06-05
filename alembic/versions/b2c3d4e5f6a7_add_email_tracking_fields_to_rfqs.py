"""add_email_tracking_fields_to_rfqs

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-04 12:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add Gmail email tracking fields to RFQ table."""
    
    op.add_column(
        'rfqs',
        sa.Column('email_thread_id', sa.String(), nullable=True)
    )
    op.add_column(
        'rfqs',
        sa.Column('email_draft_id', sa.String(), nullable=True)
    )
    op.add_column(
        'rfqs',
        sa.Column('email_sent_at', sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        'rfqs',
        sa.Column('email_status', sa.String(), nullable=True)
    )
    op.add_column(
        'rfqs',
        sa.Column('supplier_emails', JSONB(), nullable=True)
    )
    
    # Create indexes for fast lookups
    op.create_index(op.f('ix_rfqs_email_thread'), 'rfqs', ['email_thread_id'])
    op.create_index(op.f('ix_rfqs_email_status'), 'rfqs', ['email_status'])


def downgrade() -> None:
    """Remove email tracking fields from RFQ table."""
    
    op.drop_index(op.f('ix_rfqs_email_status'), table_name='rfqs')
    op.drop_index(op.f('ix_rfqs_email_thread'), table_name='rfqs')
    op.drop_column('rfqs', 'supplier_emails')
    op.drop_column('rfqs', 'email_status')
    op.drop_column('rfqs', 'email_sent_at')
    op.drop_column('rfqs', 'email_draft_id')
    op.drop_column('rfqs', 'email_thread_id')
