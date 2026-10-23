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


def _target_schema() -> str | None:
    """The schema migrations should run INTO (schema-per-app / company-DB model), or None for the legacy
    public-schema layout. Set by ``scripts/setup_db.migrate(db_name, schema=...)`` via config attributes."""
    schema = config.attributes.get("schema")
    return str(schema) if schema else None


def run_migrations_offline() -> None:
    schema = _target_schema()
    context.configure(
        url=_db_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
        version_table_schema=schema,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    # Schema-per-app model: ``version_table_schema`` keeps ``alembic_version`` in the target schema. The
    # connection's ``search_path`` is already pinned to ``<schema>,public`` via the engine's connect args
    # (see ``run_async_migrations``), so every bare-name DDL in the (unchanged) migration chain and
    # ``current_schema()`` (grant helpers, reflection) resolve to the schema; ``public`` stays on the path
    # for shared extensions. We do NOT execute on the connection here -- that would open a transaction that
    # conflicts with migrations using ``op.get_context().autocommit_block()``.
    schema = _target_schema()
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_object=include_object,
        version_table_schema=schema,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    schema = _target_schema()
    connect_args: dict[str, object] = {}
    if schema is not None:
        # Create the schema in its own connection/transaction up front (the migration connection must not
        # pre-execute), then pin the migration engine's search_path at connect time so EVERY connection it
        # opens (including the reconnect inside an autocommit_block) lands in the schema.
        pre = create_async_engine(_db_url(), poolclass=pool.NullPool)
        async with pre.begin() as conn:
            await conn.exec_driver_sql(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        await pre.dispose()
        connect_args = {"server_settings": {"search_path": f"{schema},public"}}
    connectable = create_async_engine(_db_url(), poolclass=pool.NullPool, connect_args=connect_args)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
