"""add notes_updated_at to suppliers

Revision ID: 53679d7c1322
Revises: 4ca29adca5b6
Create Date: 2026-05-27 15:21:18.520372

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '53679d7c1322'
down_revision: Union[str, Sequence[str], None] = '4ca29adca5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('suppliers', sa.Column('notes_updated_at', sa.DateTime(timezone=True), nullable=True))
    # Backfill: mark "good" notes (structured format with Products: section and length >= 100)
    # as updated today. Leave bad/missing notes with NULL so the generator picks them up.
    op.execute("""
        UPDATE suppliers
        SET notes_updated_at = NOW()
        WHERE notes IS NOT NULL
          AND LENGTH(notes) >= 100
          AND notes LIKE '%Products:%'
          AND notes NOT LIKE '%Password:%'
          AND notes NOT LIKE '%password:%'
          AND notes NOT LIKE '%Username:%'
    """)


def downgrade() -> None:
    op.drop_column('suppliers', 'notes_updated_at')
