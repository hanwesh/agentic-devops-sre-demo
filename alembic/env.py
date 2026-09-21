"""Async migrations with a transaction-scoped, database-local PostgreSQL lock."""

import asyncio
from logging.config import fileConfig

from sqlalchemy import Connection, pool, text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from src.config import settings
from src.db_options import connect_args
from src.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)
target_metadata = Base.metadata
MIGRATION_LOCK_ID = 813274491


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    if connection.dialect.name == "postgresql":
        connection.execute(text("SET LOCAL lock_timeout = '30s'"))
        connection.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": MIGRATION_LOCK_ID}
        )
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(
        settings.database_url,
        poolclass=pool.NullPool,
        hide_parameters=True,
        connect_args=connect_args(settings.database_url),
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
elif config.attributes.get("connection") is not None:
    do_run_migrations(config.attributes["connection"])
else:
    asyncio.run(run_async_migrations())
