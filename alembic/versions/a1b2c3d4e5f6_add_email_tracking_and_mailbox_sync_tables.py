"""add_email_tracking_and_mailbox_sync_tables

Revision ID: a1b2c3d4e5f6
Revises: 9a8fefe44299
Create Date: 2026-06-04 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import BIGINT

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '9a8fefe44299'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create email_tracking and mailbox_sync_cursor tables for Gmail integration."""
    
    # Create email_tracking table
    op.create_table(
        'email_tracking',
        sa.Column('id', sa.Integer(), nullable=False),
        
        # Gmail identifiers
        sa.Column('gmail_thread_id', sa.String(), nullable=False),
        sa.Column('gmail_message_id', sa.String(), nullable=True),
        sa.Column('gmail_draft_id', sa.String(), nullable=True),
        sa.Column('gmail_history_id', BIGINT(), nullable=True),
        sa.Column('gmail_label', sa.String(), nullable=True, server_default='agent-rfq'),
        
        # User & context
        sa.Column('user_email', sa.String(), nullable=False),
        
        # RFQ/Opportunity tracking
        sa.Column('rfq_id', sa.String(), nullable=False),
        sa.Column('opportunity_id', sa.String(), nullable=True),
        sa.Column('rfq_token', sa.String(), nullable=True),
        
        # Email metadata
        sa.Column('direction', sa.String(), nullable=False),  # 'draft' | 'sent' | 'received'
        sa.Column('email_type', sa.String(), nullable=False),  # 'rfq_outreach' | 'quote' | 'invoice' | etc.
        sa.Column('subject', sa.String(), nullable=True),
        sa.Column('recipient_email', sa.String(), nullable=True),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        
        # Workflow state
        sa.Column('draft_url', sa.String(), nullable=True),
        sa.Column('draft_opened_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('sent_confirmed', sa.Boolean(), nullable=True, server_default='false'),
        
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True, server_default=sa.text('NOW()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True, server_default=sa.text('NOW()')),
        
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('gmail_message_id', name='uq_email_tracking_message_id'),
        sa.UniqueConstraint('gmail_draft_id', name='uq_email_tracking_draft_id'),
    )
    
    # Create indexes
    op.create_index(op.f('ix_email_tracking_rfq'), 'email_tracking', ['rfq_id'])
    op.create_index(op.f('ix_email_tracking_thread'), 'email_tracking', ['gmail_thread_id'])
    op.create_index(op.f('ix_email_tracking_draft'), 'email_tracking', ['gmail_draft_id'])
    op.create_index(op.f('ix_email_tracking_opportunity'), 'email_tracking', ['opportunity_id'])
    op.create_index(op.f('ix_email_tracking_user'), 'email_tracking', ['user_email'])
    
    # Create mailbox_sync_cursor table
    op.create_table(
        'mailbox_sync_cursor',
        sa.Column('user_email', sa.String(), nullable=False),
        sa.Column('last_history_id', BIGINT(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True, server_default=sa.text('NOW()')),
        
        sa.PrimaryKeyConstraint('user_email'),
    )


def downgrade() -> None:
    """Drop email tracking tables."""
    op.drop_table('mailbox_sync_cursor')
    op.drop_index(op.f('ix_email_tracking_user'), table_name='email_tracking')
    op.drop_index(op.f('ix_email_tracking_opportunity'), table_name='email_tracking')
    op.drop_index(op.f('ix_email_tracking_draft'), table_name='email_tracking')
    op.drop_index(op.f('ix_email_tracking_thread'), table_name='email_tracking')
    op.drop_index(op.f('ix_email_tracking_rfq'), table_name='email_tracking')
    op.drop_table('email_tracking')
