"""add_brands_table

Revision ID: 6a57b57944ae
Revises: 135c06b656f1
Create Date: 2026-03-18 16:34:52.017159

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '6a57b57944ae'
down_revision: Union[str, Sequence[str], None] = '135c06b656f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('brands',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('netsuite_id', sa.String(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('duplicate_of', sa.UUID(), nullable=True),
    sa.ForeignKeyConstraint(['duplicate_of'], ['brands.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('netsuite_id')
    )
    op.create_index(op.f('ix_brands_duplicate_of'), 'brands', ['duplicate_of'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_brands_duplicate_of'), table_name='brands')
    op.drop_table('brands')
