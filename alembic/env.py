from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

import os
from dotenv import load_dotenv
load_dotenv()

# Override url with environment variable
# If not present in env, default to local docker-compose default since `os.getenv` might be empty
db_url = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/eagleagent")
# Normalize to use psycopg (v3) driver — psycopg2 is not installed
if db_url.startswith("postgresql+asyncpg://"):
    db_url = db_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
elif db_url.startswith("postgresql://"):
    db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)
config.set_main_option("sqlalchemy.url", db_url)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
from includes.dashboard.models import Base
target_metadata = Base.metadata

# Tables managed externally (Chainlit data layer / LangGraph checkpointer).
# Exclude from autogenerate so Alembic doesn't try to DROP or CREATE them.
EXTERNAL_TABLES = {
    "users", "threads", "steps", "elements", "feedbacks",          # Chainlit
    "checkpoints", "checkpoint_blobs", "checkpoint_writes",        # LangGraph
    "checkpoint_migrations", "store", "store_migrations",          # LangGraph
}

# Indexes/constraints that are correct in the DB but cause spurious
# autogenerate diffs due to index-vs-constraint mismatch.
# Format: (table_name, column_name)
IGNORED_COLUMNS = {
    ("suppliers", "hubspot_id"),
}


def include_object(object, name, type_, reflected, compare_to):
    """Filter for autogenerate — skip tables we don't own and spurious diffs."""
    if type_ == "table" and name in EXTERNAL_TABLES:
        return False
    # Suppress both sides of the index/constraint mismatch for ignored columns.
    # The DB has a unique index; the model declares unique=True (constraint).
    # Alembic detects both a remove_index and an add_constraint — skip them.
    if type_ in ("index", "unique_constraint") and hasattr(object, "columns"):
        table = getattr(object, "table", None)
        if table is not None:
            cols = {c.name for c in object.columns}
            if any((table.name, c) in IGNORED_COLUMNS for c in cols):
                return False
    return True


def _op_references_ignored_column(op):
    """Return True if an autogenerate op targets an ignored column."""
    # add/remove index
    if hasattr(op, "index") and op.index is not None:
        table = getattr(op.index, "table", None)
        if table is not None:
            cols = {c.name for c in op.index.columns}
            if any((table.name, c) in IGNORED_COLUMNS for c in cols):
                return True
    # add/remove constraint
    if hasattr(op, "constraint") and op.constraint is not None:
        table = getattr(op.constraint, "table", None)
        if table is not None:
            cols = {c.name for c in op.constraint.columns}
            if any((table.name, c) in IGNORED_COLUMNS for c in cols):
                return True
    return False


def process_revision_directives(context, revision, directives):
    """Strip spurious operations for columns with known index/constraint mismatches."""
    script = directives[0]
    for upgrade_ops in script.upgrade_ops_list:
        upgrade_ops.ops[:] = [
            op for op in upgrade_ops.ops if not _op_references_ignored_column(op)
        ]
    for downgrade_ops in script.downgrade_ops_list:
        downgrade_ops.ops[:] = [
            op for op in downgrade_ops.ops if not _op_references_ignored_column(op)
        ]


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        process_revision_directives=process_revision_directives,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            process_revision_directives=process_revision_directives,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
