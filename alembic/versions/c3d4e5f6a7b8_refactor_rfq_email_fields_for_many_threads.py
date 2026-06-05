"""refactor_rfq_email_fields_for_many_threads

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-04 14:45:00.000000

Refactor RFQ email fields to reflect one-RFQ-to-many-threads design.
- Remove email_thread_id (single value doesn't work for multiple threads)
- Remove email_draft_id (belongs in email_tracking only)
- Rename email_sent_at to last_email_sent_at (for clarity)
- Keep supplier_emails and email_status for summary/denormalization

The email_tracking table remains the source of truth for all email lifecycle events.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop single-thread fields and rename email_sent_at to last_email_sent_at."""
    
    # Drop the email_thread_id index and column
    op.drop_index(op.f('ix_rfqs_email_thread'), table_name='rfqs')
    op.drop_column('rfqs', 'email_thread_id')
    
    # Drop the email_draft_id column (no index to drop for this one)
    op.drop_column('rfqs', 'email_draft_id')
    
    # Rename email_sent_at to last_email_sent_at
    op.alter_column(
        'rfqs',
        'email_sent_at',
        new_column_name='last_email_sent_at'
    )
    
    # Update the index name for clarity
    op.create_index(
        op.f('ix_rfqs_last_email_sent_at'),
        'rfqs',
        ['last_email_sent_at']
    )


def downgrade() -> None:
    """Rollback: restore email_thread_id, email_draft_id, rename back to email_sent_at."""
    
    # Rename last_email_sent_at back to email_sent_at
    op.alter_column(
        'rfqs',
        'last_email_sent_at',
        new_column_name='email_sent_at'
    )
    
    # Drop the new index
    op.drop_index(op.f('ix_rfqs_last_email_sent_at'), table_name='rfqs')
    
    # Restore email_draft_id column
    op.add_column(
        'rfqs',
        sa.Column('email_draft_id', sa.String(), nullable=True)
    )
    
    # Restore email_thread_id column
    op.add_column(
        'rfqs',
        sa.Column('email_thread_id', sa.String(), nullable=True)
    )
    
    # Restore the email_thread_id index
    op.create_index(op.f('ix_rfqs_email_thread'), 'rfqs', ['email_thread_id'])
