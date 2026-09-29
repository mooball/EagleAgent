"""Add normalised brand-name indexes for match_brands

``match_brands`` (used by the Items tab brand badges and the Quotation tab's
NetSuite readiness) matched on
``regexp_replace(name, '[^a-zA-Z0-9]', '', 'g') ILIKE ?`` plus a substring
variant — neither could use an index, so every call scanned the whole brands
table (~31k rows), and the pass ran twice per RFQ render.

- ``idx_brands_name_norm_ci`` is a btree on the lower-cased normalised name,
  served by the equality pass.
- ``idx_brands_name_norm_trgm`` is a pg_trgm GIN index on the same expression,
  served by the ``LIKE '%…%'`` substring pass.

Revision ID: c1d2e3f4a5b6
Revises: d1e2f3a4b5c6
Create Date: 2026-09-29 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1d2e3f4a5b6'
down_revision: Union[str, Sequence[str], None] = 'd1e2f3a4b5c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS idx_brands_name_norm_ci "
        "ON brands (regexp_replace(lower(name), '[^a-z0-9]', '', 'g'))"
    ))
    # pg_trgm powers the LIKE '%…%' substring pass. Verified available (and the
    # app user is superuser) on both the local and production databases.
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS idx_brands_name_norm_trgm "
        "ON brands USING gin "
        "(regexp_replace(lower(name), '[^a-z0-9]', '', 'g') gin_trgm_ops)"
    ))


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS idx_brands_name_norm_trgm"))
    op.execute(sa.text("DROP INDEX IF EXISTS idx_brands_name_norm_ci"))
