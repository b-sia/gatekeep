import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import gatekeep.storage.models  # noqa: F401  (register tables)
from gatekeep.config import get_settings
from gatekeep.storage.db import Base

config = context.config

# Migrations connect with the same async driver (asyncpg) and the same
# DATABASE_URL as the app, so any driver-specific URL options (e.g.
# `?ssl=require`) mean the same thing to both. alembic.ini's sqlalchemy.url is
# deliberately unused.
database_url = get_settings().database_url

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit migration SQL to stdout without connecting (`alembic upgrade --sql`)."""
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run the migrations on a sync-facing connection.

    Called through `AsyncConnection.run_sync`, which hands Alembic the
    synchronous Connection API it expects on top of the asyncpg connection.

    Args:
        connection: The connection to run migrations on.
    """
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Connect to DATABASE_URL with asyncpg and apply the migrations."""
    connectable = create_async_engine(database_url, poolclass=pool.NullPool)
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
