"""Alembic environment. Migrations always run as the OWNER role (DB_USER), never as talent_app.

The target database URL comes from ``config.attributes["db_url"]`` (set by scripts/setup_db.py and the
test harness) or falls back to ``Settings.owner_db_url()`` (DB_NAME from .env).
Helpers for migrations (``enable_rls``, ``create_app_role``) live in ``app/core/migration_helpers.py``.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import app.models  # noqa: F401  - registers every domain's models on Base.metadata
import app.models.fairness  # noqa: F401  - Epic 49 (AD-47): isolated, not imported by app.models
from alembic import context
from app.core.models import Base
from app.core.settings import get_settings

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# Documented cross-module index exceptions: created by migrations on other domains' tables and intentionally
# not declared on those models -- reporting read-model indexes (20261001_112, ``ix_rpt_*``) and search
# trigram / expression indexes (20261001_51, ``ix_search_*``). Autogenerate must not propose dropping them.
CROSS_MODULE_INDEX_PREFIXES = ("ix_rpt_", "ix_search_")


def include_object(
    obj: object,  # noqa: ARG001 - alembic hook signature
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: object | None,
) -> bool:
    return not (
        type_ == "index"
        and reflected
        and compare_to is None
        and name is not None
        and name.startswith(CROSS_MODULE_INDEX_PREFIXES)
    )


def _db_url() -> str:
    return str(config.attributes.get("db_url") or get_settings().owner_db_url())


def run_migrations_offline() -> None:
    context.configure(
        url=_db_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = create_async_engine(_db_url(), poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
