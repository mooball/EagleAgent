"""rename rfq_items.status to match

Revision ID: i5j6k7l8m9n0
Revises: h4i5j6k7l8m9
Create Date: 2026-06-24 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'i5j6k7l8m9n0'
down_revision: Union[str, Sequence[str], None] = 'h4i5j6k7l8m9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Rename column
    op.alter_column('rfq_items', 'status', new_column_name='match',
                    existing_type=sa.String(), existing_nullable=True)

    # Drop old default (was 'unidentified') and set new default
    op.execute("ALTER TABLE rfq_items ALTER COLUMN match DROP DEFAULT")
    op.execute("ALTER TABLE rfq_items ALTER COLUMN match SET DEFAULT 'unmatched'")


def downgrade() -> None:
    # Reverse: rename back and restore old default
    op.alter_column('rfq_items', 'match', new_column_name='status',
                    existing_type=sa.String(), existing_nullable=True)
    op.execute("ALTER TABLE rfq_items ALTER COLUMN status DROP DEFAULT")
    op.execute("ALTER TABLE rfq_items ALTER COLUMN status SET DEFAULT 'unidentified'")
