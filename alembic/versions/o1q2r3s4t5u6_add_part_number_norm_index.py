"""Add functional index on normalized part_number for separator-insensitive matching

Revision ID: o1q2r3s4t5u6
Revises: n0p1q2r3s4t5
Create Date: 2026-07-23 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'o1q2r3s4t5u6'
down_revision: Union[str, Sequence[str], None] = 'n0p1q2r3s4t5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Functional index: strips non-alphanumeric chars for comparison.
    # "C50LR-BR24-16" and "C50LRBR2416" will match via this index.
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS idx_products_part_number_norm "
        "ON products (regexp_replace(part_number, '[^a-zA-Z0-9]', '', 'g'))"
    ))
    # Same for supplier_code — used in _find_product_by_code fallback.
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS idx_products_supplier_code_norm "
        "ON products (regexp_replace(supplier_code, '[^a-zA-Z0-9]', '', 'g'))"
    ))


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS idx_products_part_number_norm"))
    op.execute(sa.text("DROP INDEX IF EXISTS idx_products_supplier_code_norm"))
