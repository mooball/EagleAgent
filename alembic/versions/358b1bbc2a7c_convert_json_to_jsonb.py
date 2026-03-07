"""convert json to jsonb

Revision ID: 358b1bbc2a7c
Revises: 566b3d2890dc
Create Date: 2026-03-07 17:40:56.579919

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '358b1bbc2a7c'
down_revision: Union[str, Sequence[str], None] = '566b3d2890dc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute('ALTER TABLE users ALTER COLUMN metadata TYPE JSONB USING metadata::jsonb')
    op.execute('ALTER TABLE threads ALTER COLUMN metadata TYPE JSONB USING metadata::jsonb')
    op.execute('ALTER TABLE steps ALTER COLUMN metadata TYPE JSONB USING metadata::jsonb')
    op.execute('ALTER TABLE steps ALTER COLUMN generation TYPE JSONB USING generation::jsonb')
    op.execute('ALTER TABLE elements ALTER COLUMN props TYPE JSONB USING props::jsonb')
    op.execute('ALTER TABLE elements ALTER COLUMN "playerConfig" TYPE JSONB USING "playerConfig"::jsonb')


def downgrade() -> None:
    """Downgrade schema."""
    op.execute('ALTER TABLE elements ALTER COLUMN "playerConfig" TYPE JSON USING "playerConfig"::json')
    op.execute('ALTER TABLE elements ALTER COLUMN props TYPE JSON USING props::json')
    op.execute('ALTER TABLE steps ALTER COLUMN generation TYPE JSON USING generation::json')
    op.execute('ALTER TABLE steps ALTER COLUMN metadata TYPE JSON USING metadata::json')
    op.execute('ALTER TABLE threads ALTER COLUMN metadata TYPE JSON USING metadata::json')
    op.execute('ALTER TABLE users ALTER COLUMN metadata TYPE JSON USING metadata::json')
