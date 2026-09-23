"""Add llm_call_log

Records one row per LLM call so we can answer "which model is slow, which is
erroring, what is this workload costing" with SQL instead of grepping Railway
logs.

Why this exists
---------------
Production showed 60s chat turns where ~58s was spent waiting on Vertex, with
22 x HTTP 429 ``RESOURCE_EXHAUSTED`` in a 73-minute window — yet we had no
per-call record of model, latency, tokens or error class, so the only evidence
was a handful of stack traces. We also discovered the app-level failover was
falling back to ``gemini-2.0-flash``, a model that now returns 404, and had no
way to see it happen.

Deliberate choices
------------------
- **No cost column.** Prices change (and 3.6/3.7/3.8 are on introductory
  pricing until 2026-12-31). Cost is derived from the token columns at report
  time using a price table, so history stays meaningful.
- ``thought_tokens`` is stored separately from ``output_tokens`` because
  thinking bills at the output rate and varies wildly by model — all five Flash
  models report different thinking totals at the same nominal thinking level.
- ``service_tier`` is the tier we *requested*. Vertex does not report the tier
  that actually served the call, so a Priority->Standard graceful downgrade is
  not observable here.
- ``location`` will always be ``global`` for the current Gemini 3.x models:
  they are served from the global endpoint only, and regional locations 404.

Writes are best-effort and batched off the request path
(``includes/llm/telemetry.py``); a telemetry failure never fails a request.

Revision ID: d1e2f3a4b5c6
Revises: c7d8e9f0a1b2
Create Date: 2026-09-24 08:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd1e2f3a4b5c6'
down_revision: Union[str, Sequence[str], None] = 'c7d8e9f0a1b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'llm_call_log',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('ts', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('scope', sa.String(length=80), nullable=False),
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('model', sa.String(length=80), nullable=False),
        sa.Column('location', sa.String(length=64), nullable=True),
        sa.Column('service_tier', sa.String(length=32), nullable=True),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('ttft_ms', sa.Integer(), nullable=True),
        sa.Column('prompt_tokens', sa.Integer(), nullable=True),
        sa.Column('output_tokens', sa.Integer(), nullable=True),
        sa.Column('thought_tokens', sa.Integer(), nullable=True),
        sa.Column('total_tokens', sa.Integer(), nullable=True),
        sa.Column('attempt', sa.Integer(), nullable=True),
        sa.Column('fell_back_from', sa.String(length=80), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('error_class', sa.String(length=64), nullable=True),
        sa.Column('http_status', sa.Integer(), nullable=True),
        sa.Column('correlation_id', sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_llm_call_log_ts', 'llm_call_log', ['ts'])
    op.create_index('ix_llm_call_log_scope', 'llm_call_log', ['scope'])
    op.create_index('ix_llm_call_log_model', 'llm_call_log', ['model'])
    op.create_index('ix_llm_call_log_status', 'llm_call_log', ['status'])
    op.create_index('ix_llm_call_log_correlation_id', 'llm_call_log', ['correlation_id'])

    # The admin health panel and "what is failing now" queries both filter on a
    # recent window and group by model — this composite serves that directly.
    op.create_index(
        'ix_llm_call_log_ts_model_status',
        'llm_call_log',
        ['ts', 'model', 'status'],
    )


def downgrade() -> None:
    op.drop_index('ix_llm_call_log_ts_model_status', table_name='llm_call_log')
    op.drop_index('ix_llm_call_log_correlation_id', table_name='llm_call_log')
    op.drop_index('ix_llm_call_log_status', table_name='llm_call_log')
    op.drop_index('ix_llm_call_log_model', table_name='llm_call_log')
    op.drop_index('ix_llm_call_log_scope', table_name='llm_call_log')
    op.drop_index('ix_llm_call_log_ts', table_name='llm_call_log')
    op.drop_table('llm_call_log')
