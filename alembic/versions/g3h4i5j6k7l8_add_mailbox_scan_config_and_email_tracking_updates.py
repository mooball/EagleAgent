"""add mailbox_scan_config and email_tracking updates for Phase 3

Revision ID: g3h4i5j6k7l8
Revises: f1a2b3c4d5e6
Create Date: 2026-06-11 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision: str = 'g3h4i5j6k7l8'
down_revision: Union[str, Sequence[str], None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add mailbox_scan_config table and update email_tracking for Phase 3 scanning."""

    # Create mailbox_scan_config table
    op.create_table(
        'mailbox_scan_config',
        sa.Column('user_email', sa.String(), nullable=False),
        sa.Column('scan_enabled', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('excluded_reason', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True, server_default=sa.text('NOW()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True, server_default=sa.text('NOW()')),
        sa.PrimaryKeyConstraint('user_email'),
    )

    # Add supplier_id, customer_id, match_type to email_tracking
    op.add_column('email_tracking', sa.Column('supplier_id', UUID(as_uuid=True), nullable=True))
    op.add_column('email_tracking', sa.Column('customer_id', UUID(as_uuid=True), nullable=True))
    op.add_column('email_tracking', sa.Column('match_type', sa.String(), nullable=True))

    # Make rfq_id nullable (contact-matched emails may not have an RFQ link)
    op.alter_column('email_tracking', 'rfq_id', existing_type=sa.String(), nullable=True)

    # Make email_type nullable (scanned emails may not have a known type)
    op.alter_column('email_tracking', 'email_type', existing_type=sa.String(), nullable=True)

    # Add foreign keys
    op.create_foreign_key(
        'fk_email_tracking_supplier', 'email_tracking', 'suppliers',
        ['supplier_id'], ['id'],
    )
    op.create_foreign_key(
        'fk_email_tracking_customer', 'email_tracking', 'customers',
        ['customer_id'], ['id'],
    )

    # Add indexes
    op.create_index('ix_email_tracking_supplier', 'email_tracking', ['supplier_id'])
    op.create_index('ix_email_tracking_customer', 'email_tracking', ['customer_id'])
    op.create_index(
        'ix_email_tracking_unlinked', 'email_tracking',
        ['supplier_id', 'customer_id'],
        postgresql_where=sa.text('rfq_id IS NULL'),
    )


def downgrade() -> None:
    """Remove mailbox_scan_config and email_tracking Phase 3 columns."""
    op.drop_index('ix_email_tracking_unlinked', table_name='email_tracking')
    op.drop_index('ix_email_tracking_customer', table_name='email_tracking')
    op.drop_index('ix_email_tracking_supplier', table_name='email_tracking')
    op.drop_constraint('fk_email_tracking_customer', 'email_tracking', type_='foreignkey')
    op.drop_constraint('fk_email_tracking_supplier', 'email_tracking', type_='foreignkey')
    op.alter_column('email_tracking', 'email_type', existing_type=sa.String(), nullable=False)
    op.alter_column('email_tracking', 'rfq_id', existing_type=sa.String(), nullable=False)
    op.drop_column('email_tracking', 'match_type')
    op.drop_column('email_tracking', 'customer_id')
    op.drop_column('email_tracking', 'supplier_id')
    op.drop_table('mailbox_scan_config')
