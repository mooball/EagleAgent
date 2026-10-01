"""Add supplied_part_number to rfq_items

Stores the chosen supplier's own part number when it differs from the part
number the customer requested. This lets an "alternative" part survive from
the supplier's quote through to the NetSuite push.

Two part numbers now live on an RFQ line:

    part_number          — requested (what the customer asked for)
    supplied_part_number — quoted (the chosen supplier's own number)

The "effective" part number pushed to NetSuite is supplied when set, else
requested. Supplier selection auto-fills supplied from the supplier's
``quote_part_number``; it is cleared on deselect and can be overridden by hand.

Revision ID: e7a1b2c3d4f5
Revises: d2e3f4a5b6c7
Create Date: 2026-10-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7a1b2c3d4f5'
down_revision: Union[str, Sequence[str], None] = 'd2e3f4a5b6c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'rfq_items',
        sa.Column('supplied_part_number', sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('rfq_items', 'supplied_part_number')
