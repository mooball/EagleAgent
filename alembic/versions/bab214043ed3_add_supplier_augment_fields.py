"""add_supplier_augment_fields

Revision ID: bab214043ed3
Revises: ddbd16aabcbf
Create Date: 2026-04-28 13:11:49.985664

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = 'bab214043ed3'
down_revision: Union[str, Sequence[str], None] = 'ddbd16aabcbf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add comments, supply_chain_position, terms, modified_at, modified_by to suppliers."""
    op.add_column('suppliers', sa.Column('comments', JSONB, nullable=True))
    op.add_column('suppliers', sa.Column('supply_chain_position', JSONB, nullable=True))
    op.add_column('suppliers', sa.Column('terms', sa.String(), nullable=True))
    op.add_column('suppliers', sa.Column('modified_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('suppliers', sa.Column('modified_by', sa.String(), nullable=True))


def downgrade() -> None:
    """Remove augment fields from suppliers."""
    op.drop_column('suppliers', 'modified_by')
    op.drop_column('suppliers', 'modified_at')
    op.drop_column('suppliers', 'terms')
    op.drop_column('suppliers', 'supply_chain_position')
    op.drop_column('suppliers', 'comments')
