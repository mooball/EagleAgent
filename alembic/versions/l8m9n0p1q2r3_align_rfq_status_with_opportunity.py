"""align RFQ status values with Opportunity status codes

Revision ID: l8m9n0p1q2r3
Revises: k7l8m9n0p1q2
Create Date: 2026-07-10 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'l8m9n0p1q2r3'
down_revision: Union[str, None] = 'k7l8m9n0p1q2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Align RFQ status values with Opportunity status codes (A/B/C/D).

    Mapping:
      awaiting_quotes → in_progress  (maps to Opp A: In Progress)
      completed       → closed_won   (maps to Opp C: Closed - Won)
      cancelled       → closed_lost  (maps to Opp D: Closed - Lost)
      draft, in_progress unchanged
    """
    op.execute("UPDATE rfqs SET status = 'in_progress' WHERE status = 'awaiting_quotes'")
    op.execute("UPDATE rfqs SET status = 'closed_won' WHERE status = 'completed'")
    op.execute("UPDATE rfqs SET status = 'closed_lost' WHERE status = 'cancelled'")


def downgrade() -> None:
    """Revert to old status values."""
    op.execute("UPDATE rfqs SET status = 'awaiting_quotes' WHERE status = 'in_progress'")
    op.execute("UPDATE rfqs SET status = 'completed' WHERE status = 'closed_won'")
    op.execute("UPDATE rfqs SET status = 'cancelled' WHERE status = 'closed_lost'")
