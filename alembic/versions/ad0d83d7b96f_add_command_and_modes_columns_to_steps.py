"""add command and modes columns to steps

Revision ID: ad0d83d7b96f
Revises: 02032d8c56a9
Create Date: 2026-04-12 12:36:02.487257

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'ad0d83d7b96f'
down_revision: Union[str, Sequence[str], None] = '02032d8c56a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add command and modes columns to steps table for Chainlit command support."""
    op.add_column('steps', sa.Column('command', sa.Text(), nullable=True))
    op.add_column('steps', sa.Column('modes', sa.JSON(), nullable=True))


def downgrade() -> None:
    """Remove command and modes columns from steps table."""
    op.drop_column('steps', 'modes')
    op.drop_column('steps', 'command')

    sa.UniqueConstraint('identifier', name=op.f('users_identifier_key'), postgresql_include=[], postgresql_nulls_not_distinct=False)
    )
    op.create_table('feedbacks',
    sa.Column('id', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('forId', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('threadId', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('value', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('comment', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.ForeignKeyConstraint(['threadId'], ['threads.id'], name=op.f('feedbacks_threadId_fkey'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('feedbacks_pkey'))
    )
    op.create_table('checkpoint_blobs',
    sa.Column('thread_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('checkpoint_ns', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('channel', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('version', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('type', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('blob', postgresql.BYTEA(), autoincrement=False, nullable=True),
    sa.PrimaryKeyConstraint('thread_id', 'checkpoint_ns', 'channel', 'version', name=op.f('checkpoint_blobs_pkey'))
    )
    op.create_index(op.f('checkpoint_blobs_thread_id_idx'), 'checkpoint_blobs', ['thread_id'], unique=False)
    # ### end Alembic commands ###
