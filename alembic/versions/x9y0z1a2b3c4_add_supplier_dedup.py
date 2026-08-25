"""add supplier dedup schema

Revision ID: x9y0z1a2b3c4
Revises: w8x9y0z1a2b3
Create Date: 2026-08-25

Track A (supplier dedup) — Phase S0:
  - suppliers.use_instead: superseded row points at the supplier to use instead
  - supplier_match_keys: one row per searchable name/domain key per supplier
  - supplier_duplicate_candidates: human review queue for scan results
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'x9y0z1a2b3c4'
down_revision: Union[str, Sequence[str], None] = 'w8x9y0z1a2b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. use_instead — self-FK, mirrors Brand.duplicate_of direction
    op.add_column(
        'suppliers',
        sa.Column('use_instead', postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        'fk_suppliers_use_instead', 'suppliers', 'suppliers',
        ['use_instead'], ['id'],
    )
    op.create_index('ix_suppliers_use_instead', 'suppliers', ['use_instead'], unique=False)

    # 2. supplier_match_keys — one row per searchable key per supplier
    op.create_table(
        'supplier_match_keys',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('supplier_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('key_type', sa.String(10), nullable=False),   # 'name' | 'domain'
        sa.Column('key_value', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ['supplier_id'], ['suppliers.id'], ondelete='CASCADE',
            name='fk_smk_supplier',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('supplier_id', 'key_type', 'key_value', name='uq_supplier_match_key'),
    )
    op.create_index('ix_smk_supplier_id', 'supplier_match_keys', ['supplier_id'], unique=False)
    op.create_index('ix_smk_type_value', 'supplier_match_keys', ['key_type', 'key_value'], unique=False)
    op.execute(
        "CREATE INDEX ix_smk_name_trgm ON supplier_match_keys "
        "USING gin (key_value gin_trgm_ops) WHERE key_type = 'name';"
    )

    # 3. supplier_duplicate_candidates — human review queue
    op.create_table(
        'supplier_duplicate_candidates',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('primary_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('duplicate_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('source', sa.String(10), nullable=False, server_default='auto'),  # 'auto' | 'manual'
        sa.Column('status', sa.String(10), nullable=False, server_default='proposed'),  # proposed|merged|rejected
        sa.Column('confidence', sa.Float(), nullable=True),
        sa.Column('reasons', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_by', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('decided_by', sa.String(), nullable=True),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ['primary_id'], ['suppliers.id'], ondelete='CASCADE',
            name='fk_sdc_primary',
        ),
        sa.ForeignKeyConstraint(
            ['duplicate_id'], ['suppliers.id'], ondelete='CASCADE',
            name='fk_sdc_duplicate',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('primary_id', 'duplicate_id', name='uq_dup_candidate_pair'),
    )
    op.create_index('ix_sdc_status', 'supplier_duplicate_candidates', ['status'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_sdc_status', table_name='supplier_duplicate_candidates')
    op.drop_table('supplier_duplicate_candidates')

    op.execute("DROP INDEX IF EXISTS ix_smk_name_trgm;")
    op.drop_index('ix_smk_type_value', table_name='supplier_match_keys')
    op.drop_index('ix_smk_supplier_id', table_name='supplier_match_keys')
    op.drop_table('supplier_match_keys')

    op.drop_index('ix_suppliers_use_instead', table_name='suppliers')
    op.drop_constraint('fk_suppliers_use_instead', 'suppliers', type_='foreignkey')
    op.drop_column('suppliers', 'use_instead')
