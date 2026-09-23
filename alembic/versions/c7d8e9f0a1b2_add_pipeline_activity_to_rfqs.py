"""Add pipeline_activity to rfqs

Records that a background pipeline is actively writing to an RFQ, so the
dashboard can lock the RFQ read-only and show progress while it runs.

The RFQ-creation pipeline creates the RFQ in stage 2 (deliberately — it lets
the user navigate between Gmail and the dashboard), then spends 30s-2min in
stage 3 adding items and setting title/notes. Nothing told the dashboard this
was happening, so a user could edit a half-populated RFQ and race the
pipeline's own writes.

Shape:

    {
      "kind": "rfq_creation",
      "step": "extracting_items" | "adding_items" | "updating_details",
      "started_at": "2026-09-23T01:23:45+00:00",
      "heartbeat_at": "2026-09-23T01:24:10+00:00"
    }

Reads treat a flag whose ``heartbeat_at`` is older than
``_PIPELINE_STALE_SECONDS`` (600s) as absent, so a crashed daemon thread
self-heals the lock rather than locking the RFQ permanently.

Kept on ``rfqs`` rather than ``email_tracking`` because the items view already
has the RFQ in hand, and because the add-on path pre-creates the RFQ and passes
only ``rfq_number`` into the pipeline.

Revision ID: c7d8e9f0a1b2
Revises: f9e8d7c6b5a4
Create Date: 2026-09-23 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c7d8e9f0a1b2'
down_revision: Union[str, Sequence[str], None] = 'f9e8d7c6b5a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'rfqs',
        sa.Column('pipeline_activity', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('rfqs', 'pipeline_activity')
