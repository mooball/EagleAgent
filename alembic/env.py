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
from includes.db_models import Base
target_metadata = Base.metadata

# Tables managed externally (Chainlit data layer / LangGraph checkpointer).
# Exclude from autogenerate so Alembic doesn't try to DROP or CREATE them.
EXTERNAL_TABLES = {
    "users", "threads", "steps", "elements", "feedbacks",          # Chainlit
    "checkpoints", "checkpoint_blobs", "checkpoint_writes",        # LangGraph
    "checkpoint_migrations", "store", "store_migrations",          # LangGraph
}


def include_object(object, name, type_, reflected, compare_to):
    """Filter for autogenerate — skip tables we don't own."""
    if type_ == "table" and name in EXTERNAL_TABLES:
        return False
    return True


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
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
