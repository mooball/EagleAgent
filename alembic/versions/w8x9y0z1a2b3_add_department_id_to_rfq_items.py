"""add department_id to rfq_items

Revision ID: w8x9y0z1a2b3
Revises: v7w8x9y0z1a2
Create Date: 2026-08-19

Phase 3 — Departments on RFQ line items. department_id stores a NetSuite
department internal ID validated against the Department enum in
includes/netsuite/departments.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'w8x9y0z1a2b3'
down_revision: Union[str, None] = 'v7w8x9y0z1a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('rfq_items', sa.Column('department_id', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('rfq_items', 'department_id')
