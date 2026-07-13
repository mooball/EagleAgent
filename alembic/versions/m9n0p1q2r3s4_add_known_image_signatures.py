"""add known_image_signatures table

Revision ID: m9n0p1q2r3s4
Revises: l8m9n0p1q2r3
Create Date: 2026-07-13 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'm9n0p1q2r3s4'
down_revision = 'l8m9n0p1q2r3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'known_image_signatures',
        sa.Column('sha256', sa.String(64), primary_key=True),
        sa.Column('classification', sa.String(), nullable=False),
        sa.Column('sample_filename', sa.String(), nullable=True),
        sa.Column('source_email_id', sa.Integer(), nullable=True),
        sa.Column('size_bytes', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_table('known_image_signatures')
