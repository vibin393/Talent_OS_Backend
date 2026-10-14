"""Database engines and sessions.

* The API and workers connect as the runtime role ``talent_app`` (NOSUPERUSER NOBYPASSRLS).
* Migrations and admin scripts connect as the owner role (``DB_USER``); see ``owner_engine``.
* Tenant context: a session created by ``tenant_session(tenant_id)`` runs
  ``set_config('app.tenant_id', <id>, true)`` (== ``SET LOCAL app.tenant_id``) at the start of every
  transaction it opens, so RLS policies scope every statement to that tenant.
* Organization context (Epic 13, AD-26): the same hook sets ``app.org_kind`` / ``app.org_id`` from the
  session's ``OrgScope``. ``tenant_session`` defaults to ``OrgScope.internal()``; portal dependencies pass
  ``OrgScope.of("client" | "vendor" | "worker", org_id)``. ``platform_session`` sets no org: every
  org-isolated table returns zero rows (fail-closed).
* Eager Celery (Epic 13): tasks enqueued by after-commit handlers in eager mode are deferred until the
  session's ``commit()`` has returned (``events.flush_deferred``), so they never run inside the commit.
"""

import asyncio
import contextlib
import dataclasses
import uuid
import weakref
from collections.abc import AsyncGenerator, AsyncIterator
from contextvars import ContextVar
from typing import Annotated, Any

from fastapi import Depends
from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, SessionTransaction
from sqlalchemy.pool import NullPool

from app.core import events
from app.core.context import get_context
from app.core.settings import get_settings

TENANT_INFO_KEY = "tenant_id"
ORG_KIND_KEY = "org_kind"
ORG_ID_KEY = "org_id"
ORG_KINDS = ("internal", "client", "vendor", "worker")


@dataclasses.dataclass(frozen=True, slots=True)
class OrgScope:
    """Row visibility of org-isolated tables (``enable_org_rls``): ``internal`` sees every row; ``client`` /
    ``vendor`` / ``worker`` see only rows of their organization (client id, vendor id, candidate id)."""

    kind: str
    org_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if self.kind not in ORG_KINDS:
            msg = f"unknown org kind {self.kind!r}"
            raise ValueError(msg)
        if self.kind != "internal" and self.org_id is None:
            msg = f"org scope {self.kind!r} requires an org_id"
            raise ValueError(msg)
        if self.kind == "internal" and self.org_id is not None:
            msg = "the internal org scope carries no org_id"
            raise ValueError(msg)

    @classmethod
    def internal(cls) -> "OrgScope":
        return cls("internal")

    @classmethod
    def of(cls, kind: str, org_id: uuid.UUID | str) -> "OrgScope":
        if kind == "internal":
            msg = "use OrgScope.internal()"
            raise ValueError(msg)
        return cls(kind, uuid.UUID(str(org_id)))


class TosAsyncSession(AsyncSession):
    """``AsyncSession`` whose ``commit()`` runs the eager Celery tasks deferred by after-commit handlers once
    the commit has returned (never inside it)."""

    async def commit(self) -> None:
        self.sync_session.info[events.DEFER_INFO_KEY] = True
        await super().commit()
        events.flush_deferred(self.sync_session)


_app_engine: AsyncEngine | None = None
_app_sessionmaker: async_sessionmaker[AsyncSession] | None = None
# The event loop the process-wide pool is bound to (asyncpg connections belong to the loop that opened them).
_app_engine_loop: asyncio.AbstractEventLoop | None = None
# Any *other* loop (a Celery task's ``asyncio.run`` in a worker thread, a helper thread in eager mode, a CLI
# run after the first loop closed) gets its own NullPool engine instead of borrowing a pooled connection that
# belongs to a different (possibly closed) loop -- that fails with ``RuntimeError: no running event loop`` /
# ``'NoneType' object has no attribute 'send'``.
_LoopMakers = weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, async_sessionmaker[AsyncSession]]
_foreign_loop_makers: _LoopMakers = weakref.WeakKeyDictionary()

# Silo mode (D11 / AD-29): one engine per (event loop, tenant database). Keyed first by loop
# (asyncpg connections belong to the loop that opened them, like the pool above), then by DB name.
_TenantLoopMakers = weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, async_sessionmaker[AsyncSession]]
]
_tenant_loop_makers: _TenantLoopMakers = weakref.WeakKeyDictionary()
_tenant_engines: set[AsyncEngine] = set()  # every per-tenant engine, for dispose_engines()


def get_engine(url: str, **engine_kwargs: Any) -> AsyncEngine:
    return create_async_engine(url, **engine_kwargs)


def app_engine() -> AsyncEngine:
    """Lazily-created engine for the runtime ``talent_app`` role (one per process)."""
    global _app_engine, _app_sessionmaker  # process-wide pool, not tenant state
    if _app_engine is None:
        s = get_settings()
        _app_engine = get_engine(
            s.app_db_url(),
            echo=s.db_echo,
            pool_size=s.db_pool_size,
            max_overflow=s.db_max_overflow,
            pool_timeout=s.db_pool_timeout,
            pool_pre_ping=True,
        )
        _app_sessionmaker = async_sessionmaker(_app_engine, class_=TosAsyncSession, expire_on_commit=False)
    return _app_engine


# Per-context engine override (Epic 12): Celery tasks run each job in their own event loop (a worker's
# ``asyncio.run`` or a helper thread in eager mode), so they must not reuse the process-wide pool bound to
# another loop. ``isolated_engine()`` installs a NullPool engine for the current context only.
_override_maker: ContextVar[async_sessionmaker[AsyncSession] | None] = ContextVar(
    "talent_os_session_maker_override", default=None
)


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _foreign_loop_maker(loop: asyncio.AbstractEventLoop) -> async_sessionmaker[AsyncSession]:
    maker = _foreign_loop_makers.get(loop)
    if maker is None:
        engine = create_async_engine(get_settings().app_db_url(), poolclass=NullPool)
        maker = async_sessionmaker(engine, class_=TosAsyncSession, expire_on_commit=False)
        _foreign_loop_makers[loop] = maker
    return maker


def _sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _app_engine_loop  # the loop the process-wide pool belongs to
    override = _override_maker.get()
    if override is not None:
        return override
    loop = _running_loop()
    app_engine()
    assert _app_sessionmaker is not None
    if loop is None:
        return _app_sessionmaker
    if _app_engine_loop is None:
        _app_engine_loop = loop
    if loop is not _app_engine_loop:  # another thread's loop, or the pool's loop has closed
        return _foreign_loop_maker(loop)
    return _app_sessionmaker


def _db_sessionmaker(db_name: str) -> async_sessionmaker[AsyncSession]:
    """Silo (D11/AD-29): a sessionmaker bound to one specific database, cached per (loop, db_name)."""
    s = get_settings()
    loop = _running_loop()
    if loop is None:  # no running loop (rare): a throwaway NullPool engine
        engine = create_async_engine(s.app_db_url(db_name), poolclass=NullPool)
        _tenant_engines.add(engine)
        return async_sessionmaker(engine, class_=TosAsyncSession, expire_on_commit=False)
    per_loop = _tenant_loop_makers.get(loop)
    if per_loop is None:
        per_loop = {}
        _tenant_loop_makers[loop] = per_loop
    maker = per_loop.get(db_name)
    if maker is None:
        engine = create_async_engine(
            s.app_db_url(db_name),
            echo=s.db_echo,
            pool_size=s.db_pool_size,
            max_overflow=s.db_max_overflow,
            pool_timeout=s.db_pool_timeout,
            pool_pre_ping=True,
        )
        _tenant_engines.add(engine)
        maker = async_sessionmaker(engine, class_=TosAsyncSession, expire_on_commit=False)
        per_loop[db_name] = maker
    return maker


async def _resolve_maker(info: dict[str, Any]) -> async_sessionmaker[AsyncSession]:
    """Pick the sessionmaker for a unit of work. In pooled mode this is the shared pool; in silo
    mode a tenant's session routes to that tenant's own DB and a platform session to the platform DB."""
    override = _override_maker.get()
    if override is not None:
        return override
    if get_settings().tenancy_mode != "silo":
        return _sessionmaker()
    from app.core import tenant_registry  # local import: avoid a module-load import cycle

    tenant_id = info.get(TENANT_INFO_KEY)
    db_name = (
        await tenant_registry.resolve_db_name(tenant_id)
        if tenant_id is not None
        else get_settings().platform_db()
    )
    return _db_sessionmaker(db_name)


@contextlib.asynccontextmanager
async def isolated_engine() -> AsyncIterator[None]:
    """Use a private engine (small pool, bound to the current loop, disposed on exit) for every
    ``tenant_session`` / ``platform_session`` in this context."""
    engine = create_async_engine(get_settings().app_db_url(), pool_size=2, max_overflow=2, pool_pre_ping=True)
    token = _override_maker.set(async_sessionmaker(engine, class_=TosAsyncSession, expire_on_commit=False))
    try:
        yield
    finally:
        _override_maker.reset(token)
        await engine.dispose()


async def dispose_engines() -> None:
    global _app_engine, _app_sessionmaker, _app_engine_loop
    if _app_engine is not None:
        await _app_engine.dispose()
    _app_engine = None
    _app_sessionmaker = None
    _app_engine_loop = None
    # Silo: dispose every per-tenant engine, then the control-plane engine.
    for engine in list(_tenant_engines):
        await engine.dispose()
    _tenant_engines.clear()
    _tenant_loop_makers.clear()
    if get_settings().tenancy_mode == "silo":
        from app.core import tenant_registry

        await tenant_registry.dispose_control_engine()


def owner_engine(db_name: str | None = None, **engine_kwargs: Any) -> AsyncEngine:
    """Engine for the owner role. Migrations / admin scripts / test setup only -- never request code."""
    return get_engine(get_settings().owner_db_url(db_name), **engine_kwargs)


@event.listens_for(Session, "after_begin")
def _apply_tenant_context(session: Session, _transaction: SessionTransaction, connection: Connection) -> None:
    tenant_id = session.info.get(TENANT_INFO_KEY)
    if tenant_id is not None:
        connection.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id})
    org_kind = session.info.get(ORG_KIND_KEY)
    if org_kind is not None:
        connection.execute(
            text("SELECT set_config('app.org_kind', :kind, true), set_config('app.org_id', :oid, true)"),
            {"kind": org_kind, "oid": session.info.get(ORG_ID_KEY) or ""},
        )


@contextlib.asynccontextmanager
async def _unit_of_work(info: dict[str, Any]) -> AsyncGenerator[AsyncSession]:
    session = (await _resolve_maker(info))(info=info)
    try:
        yield session
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    finally:
        await session.close()
    events.flush_deferred(session.sync_session)


def tenant_session(
    tenant_id: uuid.UUID | str, *, org: OrgScope | None = None
) -> contextlib.AbstractAsyncContextManager[AsyncSession]:
    """Session scoped to one tenant via RLS (and to ``org``, default internal). Commits on success, rolls
    back on error."""
    scope = org if org is not None else OrgScope.internal()
    if not isinstance(scope, OrgScope):
        msg = "org must be an OrgScope"
        raise TypeError(msg)
    return _unit_of_work(
        {
            TENANT_INFO_KEY: str(uuid.UUID(str(tenant_id))),
            ORG_KIND_KEY: scope.kind,
            ORG_ID_KEY: str(scope.org_id) if scope.org_id else "",
        }
    )


def platform_session() -> contextlib.AbstractAsyncContextManager[AsyncSession]:
    """Session with no tenant context: platform (non-RLS) tables only; tenant tables return 0 rows."""
    return _unit_of_work({})


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: tenant session if the request context carries a tenant, else platform.

    Reads the context when it resolves, so it is only correct for routes whose principal dependency
    resolved first (platform-operator routes). Authenticated tenant-user routes must use
    ``app.dependencies.auth.TenantDBSessionDep``, which depends on ``current_user``.
    """
    ctx = get_context()
    if getattr(ctx.principal, "type", None) == "user":
        msg = (
            "DBSessionDep/get_db used on a tenant-user route: use "
            "app.dependencies.auth.TenantDBSessionDep (it depends on current_user)"
        )
        raise RuntimeError(msg)
    tenant_id = ctx.tenant_id
    cm = tenant_session(tenant_id) if tenant_id is not None else platform_session()
    async with cm as session:
        yield session


DBSessionDep = Annotated[AsyncSession, Depends(get_db)]
