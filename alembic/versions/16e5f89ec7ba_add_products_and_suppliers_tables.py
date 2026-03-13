"""Add products and suppliers tables

Revision ID: 16e5f89ec7ba
Revises: 358b1bbc2a7c
Create Date: 2026-03-13 13:21:39.810689

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import pgvector.sqlalchemy


# revision identifiers, used by Alembic.
revision: str = '16e5f89ec7ba'
down_revision: Union[str, Sequence[str], None] = '358b1bbc2a7c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Ensure vector extension exists
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    
    op.create_table('suppliers',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('netsuite_id', sa.String(), nullable=True),
        sa.Column('name', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('netsuite_id')
    )
    
    op.create_table('products',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('netsuite_id', sa.String(), nullable=True),
        sa.Column('part_number', sa.String(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('brand', sa.String(), nullable=True),
        sa.Column('weight_kg', sa.Float(), nullable=True),
        sa.Column('length_m', sa.Float(), nullable=True),
        sa.Column('product_type', sa.String(), nullable=True),
        sa.Column('embedding', pgvector.sqlalchemy.Vector(dim=768), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('netsuite_id')
    )
    op.create_index(op.f('ix_products_part_number'), 'products', ['part_number'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_products_part_number'), table_name='products')
    op.drop_table('products')
    op.drop_table('suppliers')
