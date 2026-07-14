"""change created_date from Date to DateTime(timezone=True) with backfill

Revision ID: n0p1q2r3s4t5
Revises: m9n0p1q2r3s4
Create Date: 2026-07-14 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'n0p1q2r3s4t5'
down_revision = 'm9n0p1q2r3s4'
branch_labels = None
depends_on = None


def upgrade():
    # Step 1: Alter column type from date → timestamp with time zone
    # PostgreSQL can auto-cast date → timestamptz by assuming midnight UTC
    op.alter_column(
        'rfqs', 'created_date',
        type_=sa.DateTime(timezone=True),
        existing_type=sa.Date(),
        existing_nullable=False,
    )

    # Step 2: Backfill — where created_date and updated_at fall on the same day,
    # use updated_at as the creation timestamp (much more accurate than midnight UTC)
    op.execute("""
        UPDATE rfqs
        SET created_date = updated_at
        WHERE updated_at IS NOT NULL
          AND created_date::date = updated_at::date
    """)


def downgrade():
    # Cast back to date (drops time component)
    op.alter_column(
        'rfqs', 'created_date',
        type_=sa.Date(),
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=False,
    )
