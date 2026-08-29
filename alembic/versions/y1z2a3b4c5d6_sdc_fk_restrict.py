"""supplier_duplicate_candidates: FK CASCADE → RESTRICT/SET NULL + duplicate_name

Merge flow: deleting a web duplicate supplier used to cascade-delete its
candidate rows at the DB level, so the merge route's later UPDATE on the
candidate row hit "0 rows matched" (StaleDataError → 500) and the review
queue silently lost decision history.

Now:
- primary_id FK is RESTRICT (the primary supplier is never deleted).
- duplicate_id FK is SET NULL and the column is nullable, so deleting the
  web duplicate keeps the decided row; duplicate_name snapshots the name
  for the history record.
- merge_suppliers remaps/deletes queue rows explicitly before the supplier
  row is removed.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'y1z2a3b4c5d6'
down_revision: str = 'x9y0z1a2b3c4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint('fk_sdc_primary', 'supplier_duplicate_candidates', type_='foreignkey')
    op.drop_constraint('fk_sdc_duplicate', 'supplier_duplicate_candidates', type_='foreignkey')
    op.alter_column(
        'supplier_duplicate_candidates', 'duplicate_id',
        existing_type=postgresql.UUID(as_uuid=True), nullable=True,
    )
    op.add_column('supplier_duplicate_candidates',
                  sa.Column('duplicate_name', sa.String(), nullable=True))
    op.create_foreign_key(
        'fk_sdc_primary', 'supplier_duplicate_candidates', 'suppliers',
        ['primary_id'], ['id'], ondelete='RESTRICT',
    )
    op.create_foreign_key(
        'fk_sdc_duplicate', 'supplier_duplicate_candidates', 'suppliers',
        ['duplicate_id'], ['id'], ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint('fk_sdc_primary', 'supplier_duplicate_candidates', type_='foreignkey')
    op.drop_constraint('fk_sdc_duplicate', 'supplier_duplicate_candidates', type_='foreignkey')
    op.drop_column('supplier_duplicate_candidates', 'duplicate_name')
    op.alter_column(
        'supplier_duplicate_candidates', 'duplicate_id',
        existing_type=postgresql.UUID(as_uuid=True), nullable=False,
    )
    op.create_foreign_key(
        'fk_sdc_primary', 'supplier_duplicate_candidates', 'suppliers',
        ['primary_id'], ['id'], ondelete='CASCADE',
    )
    op.create_foreign_key(
        'fk_sdc_duplicate', 'supplier_duplicate_candidates', 'suppliers',
        ['duplicate_id'], ['id'], ondelete='CASCADE',
    )
