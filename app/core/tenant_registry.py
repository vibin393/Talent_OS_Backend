"""Control-plane tenant registry for the DB-per-tenant (silo) model — D11 / AD-29.

The registry is the one cross-tenant store: it maps a ``tenant_id`` to the database that
holds that tenant's data (plus its cell and migrated schema version). It lives in its own
control-plane database (``settings.control_db_name``), never inside a tenant DB.

(Distinct from ``app.core.tenancy``, which classifies tables as platform/mixed/tenant for RLS.)

* The request path calls :func:`resolve_db_name` (cached) to pick a tenant's DB; see
  ``app.core.database.tenant_session`` in silo mode.
* The provisioner / migration runner calls :func:`register_tenant`, :func:`list_tenants`,
  and :func:`ensure_registry_schema`.

The control engine connects as the owner role: resolving a DSN is control-plane infrastructure,
not tenant data, so it is deliberately outside the ``talent_app`` RLS world.
"""

import dataclasses
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.settings import get_settings

# Statuses a tenant DB may be routed to at request time.
LIVE_STATUSES = ("active", "provisioning", "suspended")

# One tenant has a DIFFERENT database in each app (Talent OS, Croar, …), so the registry keys on
# (tenant_id, service). This process is the Talent OS service.
SERVICE = "talentos"

_control_engine: AsyncEngine | None = None
# tenant_id (str) -> (db_name, schema_name) for THIS service. ``schema_name`` is None for the legacy
# dedicated-DB model and the schema name (e.g. "talentos") for the company-DB / schema-per-app model.
# Stable for a tenant's lifetime; invalidated on register.
_db_target_cache: dict[str, tuple[str, str | None]] = {}


@dataclasses.dataclass(frozen=True, slots=True)
class TenantRecord:
    tenant_id: uuid.UUID
    slug: str
    db_name: str
    cell: str
    schema_version: str | None
    status: str
    schema_name: str | None = None


def control_engine() -> AsyncEngine:
    """Lazily-created owner-role engine against the control-plane DB (NullPool: low traffic)."""
    global _control_engine
    if _control_engine is None:
        s = get_settings()
        _control_engine = create_async_engine(
            s.owner_db_url(s.control_db_name), poolclass=NullPool, pool_pre_ping=True
        )
    return _control_engine


async def dispose_control_engine() -> None:
    global _control_engine
    if _control_engine is not None:
        await _control_engine.dispose()
    _control_engine = None
    _db_target_cache.clear()


async def ensure_registry_schema() -> None:
    """Create the ``tenant_db_map`` registry table if absent (idempotent). Keyed (tenant_id, service)
    so one tenant can have a different database in each app.

    ``schema_name`` (nullable) selects the resolution path: NULL = legacy dedicated-DB model (connect to
    ``db_name``, public schema); a value = schema-per-app model (connect to ``db_name`` and set the
    connection ``search_path`` to ``<schema_name>,public``). The ``ADD COLUMN IF NOT EXISTS`` keeps an
    already-provisioned control DB working — old rows simply read back NULL."""
    async with control_engine().begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS tenant_db_map (
                    tenant_id       uuid NOT NULL,
                    service         text NOT NULL,
                    slug            text NOT NULL,
                    db_name         text NOT NULL,
                    cell            text NOT NULL DEFAULT 'local',
                    schema_version  text,
                    status          text NOT NULL DEFAULT 'provisioning',
                    created_at      timestamptz NOT NULL DEFAULT now(),
                    updated_at      timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (tenant_id, service),
                    UNIQUE (service, slug)
                )
                """
            )
        )
        await conn.execute(text("ALTER TABLE tenant_db_map ADD COLUMN IF NOT EXISTS schema_name text"))


async def register_tenant(
    tenant_id: uuid.UUID | str,
    slug: str,
    db_name: str,
    *,
    cell: str = "local",
    schema_version: str | None = None,
    status: str = "active",
    schema_name: str | None = None,
) -> None:
    """Insert or update this service's registry row for a tenant (idempotent on (tenant_id, service)).

    ``schema_name`` None => legacy dedicated-DB model; a value (e.g. ``"talentos"``) => schema-per-app
    model, so the request path binds the connection ``search_path`` to that schema."""
    tid = uuid.UUID(str(tenant_id))
    async with control_engine().begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO tenant_db_map
                    (tenant_id, service, slug, db_name, cell, schema_version, status, schema_name)
                VALUES (:tid, :svc, :slug, :db, :cell, :ver, :status, :schema)
                ON CONFLICT (tenant_id, service) DO UPDATE SET
                    slug = EXCLUDED.slug,
                    db_name = EXCLUDED.db_name,
                    cell = EXCLUDED.cell,
                    schema_version = EXCLUDED.schema_version,
                    status = EXCLUDED.status,
                    schema_name = EXCLUDED.schema_name,
                    updated_at = now()
                """
            ),
            {"tid": str(tid), "svc": SERVICE, "slug": slug, "db": db_name, "cell": cell,
             "ver": schema_version, "status": status, "schema": schema_name},
        )
    _db_target_cache[str(tid)] = (db_name, schema_name)


async def set_schema_version(tenant_id: uuid.UUID | str, version: str) -> None:
    async with control_engine().begin() as conn:
        await conn.execute(
            text(
                "UPDATE tenant_db_map SET schema_version = :v, updated_at = now() "
                "WHERE tenant_id = :tid AND service = :svc"
            ),
            {"v": version, "tid": str(uuid.UUID(str(tenant_id))), "svc": SERVICE},
        )


def _row_to_record(row: Any) -> TenantRecord:
    return TenantRecord(
        tenant_id=row.tenant_id,
        slug=row.slug,
        db_name=row.db_name,
        cell=row.cell,
        schema_version=row.schema_version,
        status=row.status,
        schema_name=getattr(row, "schema_name", None),
    )


async def list_tenants(*, only_live: bool = True) -> list[TenantRecord]:
    """This service's registered tenants (for migration fan-out). ``only_live`` filters statuses."""
    sql = (
        "SELECT tenant_id, slug, db_name, cell, schema_version, status, schema_name "
        "FROM tenant_db_map WHERE service = :svc"
    )
    params: dict[str, Any] = {"svc": SERVICE}
    if only_live:
        sql += " AND status = ANY(:statuses)"
        params["statuses"] = list(LIVE_STATUSES)
    sql += " ORDER BY created_at"
    async with control_engine().connect() as conn:
        result = await conn.execute(text(sql), params)
        return [_row_to_record(r) for r in result]


async def resolve_db_target(tenant_id: uuid.UUID | str) -> tuple[str, str | None]:
    """Return ``(db_name, schema_name)`` for this tenant FOR THIS SERVICE (cached).

    ``schema_name`` is None for the legacy dedicated-DB model (connect to ``db_name``, public schema) and
    the schema name for the company-DB / schema-per-app model (bind ``search_path`` to ``<schema>,public``).
    """
    tid = str(uuid.UUID(str(tenant_id)))
    cached = _db_target_cache.get(tid)
    if cached is not None:
        return cached
    async with control_engine().connect() as conn:
        result = await conn.execute(
            text(
                "SELECT db_name, schema_name, status FROM tenant_db_map "
                "WHERE tenant_id = :tid AND service = :svc"
            ),
            {"tid": tid, "svc": SERVICE},
        )
        row = result.first()
    if row is None:
        msg = f"tenant {tid} is not registered for service {SERVICE!r}"
        raise LookupError(msg)
    if row.status not in LIVE_STATUSES:
        msg = f"tenant {tid} is not routable (status={row.status})"
        raise LookupError(msg)
    target = (str(row.db_name), row.schema_name)
    _db_target_cache[tid] = target
    return target


async def resolve_db_name(tenant_id: uuid.UUID | str) -> str:
    """Return the DB name that holds this tenant's data FOR THIS SERVICE (cached).

    Legacy helper kept for callers that only need the database (e.g. internal integration). New request-path
    code should use :func:`resolve_db_target` to also obtain the schema."""
    db_name, _schema = await resolve_db_target(tenant_id)
    return db_name
