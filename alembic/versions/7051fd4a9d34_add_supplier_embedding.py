"""add_supplier_embedding

Revision ID: 7051fd4a9d34
Revises: 931fa34911ec
Create Date: 2026-03-20 13:37:02.804528

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import pgvector.sqlalchemy.vector

# revision identifiers, used by Alembic.
revision: str = '7051fd4a9d34'
down_revision: Union[str, Sequence[str], None] = '931fa34911ec'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('suppliers', sa.Column('embedding', pgvector.sqlalchemy.vector.VECTOR(dim=256), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('suppliers', 'embedding')
