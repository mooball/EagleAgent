"""alter_vector_256

Revision ID: 135c06b656f1
Revises: 903ddb525ddd
Create Date: 2026-03-13 14:28:37.170993

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '135c06b656f1'
down_revision: Union[str, Sequence[str], None] = '903ddb525ddd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TABLE products ALTER COLUMN embedding TYPE vector(256);")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE products ALTER COLUMN embedding TYPE vector(768);")
