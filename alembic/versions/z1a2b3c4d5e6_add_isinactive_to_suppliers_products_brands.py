"""add isinactive to suppliers, products, brands

Sync the NetSuite inactive flag locally. Previously the sync queries
filtered inactive records out entirely, so rows deactivated in NetSuite
never got flagged locally. Columns default false; the model uses a
client-side default for new rows.
"""
from alembic import op
import sqlalchemy as sa

revision: str = 'z1a2b3c4d5e6'
down_revision: str = 'y1z2a3b4c5d6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('suppliers', sa.Column(
        'isinactive', sa.Boolean(), nullable=False, server_default=sa.text('false')))
    op.add_column('products', sa.Column(
        'isinactive', sa.Boolean(), nullable=False, server_default=sa.text('false')))
    op.add_column('brands', sa.Column(
        'isinactive', sa.Boolean(), nullable=False, server_default=sa.text('false')))


def downgrade() -> None:
    op.drop_column('brands', 'isinactive')
    op.drop_column('products', 'isinactive')
    op.drop_column('suppliers', 'isinactive')
