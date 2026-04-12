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
    sa.Column('createdAt', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('name', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('userId', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('userIdentifier', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('tags', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.ForeignKeyConstraint(['userId'], ['users.id'], name=op.f('threads_userId_fkey'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('threads_pkey'))
    )
    op.create_table('store_migrations',
    sa.Column('v', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('v', name=op.f('store_migrations_pkey'))
    )
    op.create_table('checkpoint_migrations',
    sa.Column('v', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('v', name=op.f('checkpoint_migrations_pkey'))
    )
    op.create_table('elements',
    sa.Column('id', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('threadId', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('type', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('url', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('chainlitKey', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('name', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('display', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('objectKey', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('size', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('page', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('language', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('forId', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('mime', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('props', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('autoPlay', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('playerConfig', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.ForeignKeyConstraint(['threadId'], ['threads.id'], name=op.f('elements_threadId_fkey'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('elements_pkey'))
    )
    op.create_table('store',
    sa.Column('prefix', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('key', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('value', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), autoincrement=False, nullable=True),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), autoincrement=False, nullable=True),
    sa.Column('expires_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('ttl_minutes', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.PrimaryKeyConstraint('prefix', 'key', name=op.f('store_pkey'))
    )
    op.create_index(op.f('store_prefix_idx'), 'store', ['prefix'], unique=False, postgresql_ops={'prefix': 'text_pattern_ops'})
    op.create_index(op.f('idx_store_expires_at'), 'store', ['expires_at'], unique=False, postgresql_where='(expires_at IS NOT NULL)')
    op.create_table('checkpoints',
    sa.Column('thread_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('checkpoint_ns', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('checkpoint_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('parent_checkpoint_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('type', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('checkpoint', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=False),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('thread_id', 'checkpoint_ns', 'checkpoint_id', name=op.f('checkpoints_pkey'))
    )
    op.create_index(op.f('checkpoints_thread_id_idx'), 'checkpoints', ['thread_id'], unique=False)
    op.create_table('steps',
    sa.Column('id', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('name', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('type', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('threadId', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('parentId', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('streaming', sa.BOOLEAN(), autoincrement=False, nullable=False),
    sa.Column('waitForAnswer', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('isError', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('tags', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('input', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('output', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('createdAt', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('start', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('end', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('generation', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('showInput', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('language', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('defaultOpen', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=True),
    sa.Column('command', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('modes', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.ForeignKeyConstraint(['threadId'], ['threads.id'], name=op.f('steps_threadId_fkey'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('steps_pkey'))
    )
    op.create_table('users',
    sa.Column('id', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('identifier', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=False),
    sa.Column('createdAt', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('users_pkey')),
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
