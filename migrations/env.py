from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, make_url, pool

import gatekeep.storage.models  # noqa: F401  (register tables)
from gatekeep.config import get_settings
from gatekeep.storage.db import Base

config = context.config

# Alembic runs synchronously; convert the async URL to a sync psycopg2 one.
# The driver is named explicitly because a bare "postgresql://" resolves to
# psycopg (v3) on SQLAlchemy 2.1+, which isn't installed.
sync_url = make_url(get_settings().database_url).set(drivername="postgresql+psycopg2")
config.set_main_option(
    "sqlalchemy.url",
    sync_url.render_as_string(hide_password=False).replace("%", "%%"),
)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
