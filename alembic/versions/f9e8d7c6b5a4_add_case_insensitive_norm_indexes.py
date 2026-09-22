"""Add case-insensitive normalised part number / supplier code indexes

The indexes added in o1q2r3s4t5u6 are on the raw expression
``regexp_replace(col, '[^a-zA-Z0-9]', '', 'g')``. Every call site compares with
``ILIKE``, which a plain btree index cannot serve — so the planner falls back to
a sequential scan of the whole products table (305k rows) and
``_find_product_by_code`` measured 260-540ms per call.

These new indexes are on ``regexp_replace(lower(col), '[^a-z0-9]', '', 'g')``,
which is the same shape already used by ``_BRAND_FAMILY_SQL`` for brands. An
equality comparison against a value normalised the same way is index-usable and
returns in ~1ms.

The old case-sensitive indexes are intentionally left in place for now; they are
unused by current code and can be dropped in a follow-up once proven cold.

Revision ID: f9e8d7c6b5a4
Revises: e6f7a8b9c0d1
Create Date: 2026-09-22 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f9e8d7c6b5a4'
down_revision: Union[str, Sequence[str], None] = 'e6f7a8b9c0d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Case-insensitive functional indexes: lower() first, then strip separators.
    # "c50lr-br24-16", "C50LR-BR24-16" and "C50LRBR2416" all normalise to
    # "c50lrbr2416" and match via these indexes.
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS idx_products_part_number_norm_ci "
        "ON products (regexp_replace(lower(part_number), '[^a-z0-9]', '', 'g'))"
    ))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS idx_products_supplier_code_norm_ci "
        "ON products (regexp_replace(lower(supplier_code), '[^a-z0-9]', '', 'g'))"
    ))


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS idx_products_part_number_norm_ci"))
    op.execute(sa.text("DROP INDEX IF EXISTS idx_products_supplier_code_norm_ci"))
