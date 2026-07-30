"""rename_rfq_notes_to_title

Revision ID: p1q2r3s4t5u6
Revises: o1q2r3s4t5u6
Create Date: 2026-07-30 12:00:00.000000

Rename RFQ.notes to RFQ.title (it was always used as the title/description).
Add a new RFQ.notes column (Text) for customer requirements/notes.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'p1q2r3s4t5u6'
down_revision: Union[str, Sequence[str], None] = 'o1q2r3s4t5u6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Rename existing 'notes' column to 'title' (contains the RFQ title/description)
    op.alter_column('rfqs', 'notes', new_column_name='title',
                    existing_type=sa.Text(), type_=sa.String(), nullable=True)
    # Add new 'notes' column for customer requirements/delivery notes
    op.add_column('rfqs', sa.Column('notes', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('rfqs', 'notes')
    op.alter_column('rfqs', 'title', new_column_name='notes',
                    existing_type=sa.String(), type_=sa.Text(), nullable=True)
