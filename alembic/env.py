import asyncio
from logging.config import fileConfig

import app.models  # noqa: F401
from alembic import context
from app.core.config import settings
from app.db.base import Base
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", settings.database_url)
target_metadata = Base.metadata


def _dialect_name() -> str:
    return settings.database_url.split("://", 1)[0].split("+", 1)[0]


def _include_object(object_, name, type_, reflected: bool, compare_to) -> bool:
    """Keep autogenerate aligned with what the running dialect can express.

    ``uq_payments_single_success_per_booking`` is declared with
    ``ddl_if(dialect="postgresql")``. Comparing schemas on SQLite would always
    report it as missing, so indexes scoped to another dialect are ignored.
    """
    if type_ != "index":
        return True
    ddl_if = getattr(object_, "_ddl_if", None)
    return ddl_if is None or ddl_if.dialect == _dialect_name()


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
