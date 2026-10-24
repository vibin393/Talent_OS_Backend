"""Self-serve company onboarding for the unified platform.

``provision_company`` is the one reusable entry point the public signup endpoint calls. It:

  1. validates the slug (``^[a-z0-9-]{2,40}$``) and checks it is free across every app in the control
     registry (``tenant_db_map``);
  2. provisions the Talent OS tenant — creates ``tenant_<slug>``, migrates it to head and registers it
     in the control plane for ``service=talentos`` (the core of ``scripts/provision_tenant.py``, factored
     here so the endpoint never shells out);
  3. registers the other apps (Croar / Feedback IQ) in the control plane — in the ``schema_per_app`` model as
     sibling schemas (``croar`` / ``feedbackiq``) inside the one ``company_<slug>`` DB, in the legacy
     ``db_per_tenant`` model as their own EMPTY databases (``croar_tenant_<slug>`` / ``feedbackiq_tenant_<slug>``);
     either way those apps build their own tables on first access, so no migrations run here;
  4. seeds the ``Tenant`` row (platform + the tenant DB's self-row), the default roles and an
     ``enterprise`` entitlement, and creates the first admin as an INVITED user (pending, no password)
     with a fresh invitation token;
  5. returns ``{tenant_slug, admin_email, invite_url}`` where ``invite_url`` points at the accept-invite
     page on the tenant's own subdomain.

Idempotent / safe on retry: every database create skips an existing one, every registry/plan write is an
upsert, the ``Tenant`` rows and roles are only inserted when missing, and a still-pending admin invitation
is reused (its token rotated) rather than duplicated.

Blocking work (``CREATE DATABASE`` + Alembic migrations, which open their own event loop) runs in a worker
thread so it never fights the request's running loop; the async registry / ORM work runs on the loop.

(Named ``company_onboarding`` to avoid colliding with the ``app.services.onboarding`` package — the Epic 23
digital-onboarding feature, which is unrelated.)
"""

import asyncio
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from loguru import logger
from sqlalchemy import select, text

from app.core import migration_helpers, tenant_registry
from app.core.errors import ConflictError, ValidationFailed
from app.core.settings import get_settings
from scripts import setup_db

SLUG_RE = re.compile(r"^[a-z0-9-]{2,40}$")
# Reserved: these would collide with infra DBs (e.g. slug "template" -> tenant_template, the copy source).
RESERVED_SLUGS = frozenset({"template", "postgres", "control", "public", "admin", "www", "api", "app"})

# Fast provisioning: a brand-new Talent OS tenant DB is created by COPYING a pre-migrated template
# database (`CREATE DATABASE ... TEMPLATE ...` — a few seconds) instead of running every Alembic
# migration from scratch (minutes). The template is built once (lazily) and reused. It is schema-only
# (never holds tenant data), so a copy is a clean, at-head tenant DB with all RLS policies/triggers.
TEMPLATE_DB = "tenant_template"
_template_built = False

# Fast provisioning for the company-DB / schema-per-app model: a brand-new ``company_<slug>`` DB is created by
# COPYING a pre-built template (``CREATE DATABASE ... TEMPLATE`` -- seconds) instead of a fresh migrate
# (minutes). The template is an empty company DB with the ``talentos`` schema pre-migrated to head, the empty
# sibling app schemas and the three per-app roles/grants. Built once (lazily) and reused.
COMPANY_TEMPLATE_DB = "company_template"
_company_template_built = False

# The other apps' silo DB naming (service -> DB-name prefix) for the LEGACY db-per-tenant model. Talent OS uses
# settings.tenant_db_name.
OTHER_SERVICE_PREFIXES: dict[str, str] = {
    "croar": "croar_tenant_",
    "feedbackiq": "feedbackiq_tenant_",
}

# Company-DB / schema-per-app model: the other apps live as sibling schemas INSIDE the one ``company_<slug>``
# DB (service -> schema name). They build their own tables inside the schema on first access, so onboarding
# only registers the control-plane row pointing at the company DB + this schema (no migrations run here).
OTHER_SERVICE_SCHEMAS: dict[str, str] = {
    "croar": "croar",
    "feedbackiq": "feedbackiq",
}

# Match whatever plan the reference tenant (acme) has so every module shows for the new workspace.
DEFAULT_PLAN = "enterprise"

# India-launch defaults for a self-serve tenant (same as scripts/provision_tenant.py).
DEFAULT_TZ = "Asia/Kolkata"
DEFAULT_CURRENCY = "INR"
DEFAULT_LOCALE = "en-IN"
DEFAULT_SEAT_LIMIT = 25


@dataclass(frozen=True, slots=True)
class ProvisionResult:
    tenant_slug: str
    admin_email: str
    invite_url: str


@dataclass(frozen=True, slots=True)
class CompanyAdminRow:
    """One company for the operator console (validated into ``schemas.company_signup.CompanyAdminOut``)."""

    id: uuid.UUID
    name: str
    slug: str
    status: str
    created_at: datetime
    plan: str | None
    admin_email: str | None


def _accept_invite_url(slug: str, token: str) -> str:
    """The admin's invitation link on the tenant's own subdomain (the accept page reads ``?token=``)."""
    return f"http://{slug}.talentos.localhost/accept-invite?token={token}"  # NOSONAR


async def _slug_is_free(slug: str) -> bool:
    """True if ``slug`` is not registered for ANY service in the control registry."""
    await tenant_registry.ensure_registry_schema()
    async with tenant_registry.control_engine().connect() as conn:
        row = (
            await conn.execute(text("SELECT 1 FROM tenant_db_map WHERE slug = :s LIMIT 1"), {"s": slug})
        ).first()
    return row is None


def _build_template_once() -> None:
    """Ensure the template DB exists and is at head, once per process (cached). ``migrate`` is always
    run (not only on first create): it's a cheap no-op when the template is already current, and it
    applies any NEW migration added after the template was first built — so the template never drifts
    behind head and copied tenants always get the latest schema. Cost is only paid when behind."""
    global _template_built
    if _template_built:
        return
    setup_db.ensure_database(TEMPLATE_DB)
    setup_db.migrate(TEMPLATE_DB)  # idempotent: no-op at head, applies the delta after a schema change
    _template_built = True


def _create_from_template(db_name: str) -> None:
    """Create ``db_name`` by copying the template DB (fast). Idempotent: skips an existing DB. Requires no
    active sessions on the template (there are none — it only ever serves as a copy source)."""

    async def _do() -> None:
        conn = await setup_db._connect("postgres")  # superuser connection to the maintenance DB
        try:
            exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", db_name)
            if not exists:
                await conn.execute(f'CREATE DATABASE "{db_name}" TEMPLATE "{TEMPLATE_DB}"')
        finally:
            await conn.close()

    asyncio.run(_do())


def _provision_talentos_db(db_name: str) -> None:
    """Blocking (worker thread): ensure the template exists, then create ``tenant_<slug>`` by copying it —
    seconds instead of minutes, with the full at-head schema (RLS policies/triggers included)."""
    _build_template_once()
    _create_from_template(db_name)
    logger.info("[company_onboarding] talentos DB {} provisioned from template", db_name)


def _apply_company_roles(db_name: str) -> None:
    """Create (idempotently) the three per-app restricted roles and their schema-scoped grants/revokes inside
    ``db_name`` (runs as the owner role). Also (re)grants CONNECT on ``db_name`` -- a ``CREATE DATABASE ...
    TEMPLATE`` copy carries the schema ACLs but NOT the per-database CONNECT grant, so this is re-run on every
    freshly copied company DB. See ``migration_helpers.company_role_isolation_sql``."""
    s = get_settings()
    stmts = migration_helpers.company_role_isolation_sql(
        db_name,
        migration_helpers.COMPANY_APP_SCHEMA_ROLES,
        s.db_app_password,
        dml_schemas=(s.talentos_schema,),
    )

    async def _do() -> None:
        conn = await setup_db._connect(db_name)  # owner connection to the company DB
        try:
            for stmt in stmts:
                await conn.execute(stmt)
        finally:
            await conn.close()

    asyncio.run(_do())


def _build_company_template_once() -> None:
    """Build the company-DB template once per process (cached). An empty ``company_template`` DB with the
    ``talentos`` schema migrated to head, the empty ``croar`` / ``feedbackiq`` sibling schemas and the three
    per-app roles/grants. ``migrate`` is always run (cheap no-op at head) so the template never drifts behind
    a newly added migration -- the delta cost is paid here, not per company."""
    global _company_template_built
    if _company_template_built:
        return
    s = get_settings()
    setup_db.ensure_database(COMPANY_TEMPLATE_DB)
    setup_db.migrate(COMPANY_TEMPLATE_DB, schema=s.talentos_schema)
    _apply_company_roles(COMPANY_TEMPLATE_DB)  # also creates the empty croar/feedbackiq schemas
    _company_template_built = True


def _create_company_from_template(db_name: str) -> bool:
    """Create ``db_name`` by copying ``company_template`` (fast). Returns True on a fresh template copy, False
    if the DB already existed. Raises on any failure so the caller can fall back to a full migrate. Requires
    no active sessions on the template (nothing connects to it except this build/copy source)."""
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", db_name):
        msg = f"unsafe database name: {db_name!r}"
        raise ValueError(msg)

    async def _do() -> bool:
        conn = await setup_db._connect("postgres")  # superuser connection to the maintenance DB
        try:
            exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", db_name)
            if exists:
                return False
            await conn.execute(f'CREATE DATABASE "{db_name}" TEMPLATE "{COMPANY_TEMPLATE_DB}"')
            return True
        finally:
            await conn.close()

    return asyncio.run(_do())


def _provision_company_schema_db(db_name: str, schema: str) -> None:
    """Blocking (worker thread): schema-per-app / company-DB model.

    Provision ONE database ``company_<slug>`` holding the Talent OS tables in the ``schema`` schema at head,
    plus the three per-app restricted roles/grants. Fast path: build (once) the ``company_template`` DB and
    COPY it (``CREATE DATABASE ... TEMPLATE`` -- seconds), then run a no-op delta ``migrate`` (applies any
    migration added after the template was built) and re-assert the per-DB bits a template copy does not carry
    (CONNECT grants). If the template path fails for ANY reason (missing / stale template, active sessions,
    ...), fall back to a full fresh migrate into a newly created DB so provisioning is never blocked.

    The migration creates the schema, pins the connection ``search_path`` to it and keeps ``alembic_version``
    there too; ``public`` holds only shared extensions -- no app tables."""
    try:
        _build_company_template_once()
        copied = _create_company_from_template(db_name)
        setup_db.migrate(db_name, schema=schema)  # no-op at head; applies the delta if the template is behind
        _apply_company_roles(db_name)  # idempotent; re-grants CONNECT on the new DB (not copied by TEMPLATE)
        logger.info(
            "[company_onboarding] talentos schema provisioned via {}: {}.{}",
            "template-copy" if copied else "existing-db",
            db_name,
            schema,
        )
    except Exception:
        logger.warning(
            "[company_onboarding] company template path failed for {}; falling back to full migrate",
            db_name,
            exc_info=True,
        )
        setup_db.ensure_database(db_name)
        setup_db.migrate(db_name, schema=schema)
        _apply_company_roles(db_name)
        logger.info(
            "[company_onboarding] talentos schema provisioned via full-migrate: {}.{}", db_name, schema
        )


async def _register_service(
    tenant_id: uuid.UUID, service: str, slug: str, db_name: str, *, schema_name: str | None = None
) -> None:
    """Upsert the control-registry row for ``service`` (idempotent on (tenant_id, service)).

    ``schema_name`` is NULL for the legacy db-per-tenant model (the app connects to its own ``db_name`` on the
    public schema) and the app's schema for the company-DB model (the app resolves into ``db_name`` =
    ``company_<slug>`` and binds its ``search_path`` to ``<schema>,public``). NULL-safe upsert: an existing
    row's ``schema_name`` is overwritten with the new value (including back to NULL)."""
    await tenant_registry.ensure_registry_schema()
    async with tenant_registry.control_engine().begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO tenant_db_map
                    (tenant_id, service, slug, db_name, cell, schema_version, status, schema_name)
                VALUES (:tid, :svc, :slug, :db, 'local', NULL, 'active', :schema)
                ON CONFLICT (tenant_id, service) DO UPDATE SET
                    slug = EXCLUDED.slug, db_name = EXCLUDED.db_name,
                    status = EXCLUDED.status, schema_name = EXCLUDED.schema_name, updated_at = now()
                """
            ),
            {"tid": str(tenant_id), "svc": service, "slug": slug, "db": db_name, "schema": schema_name},
        )


async def _seed_and_invite(
    tenant_id: uuid.UUID, slug: str, company_name: str, admin_email: str, admin_name: str
) -> str:
    """Seed the Tenant row + default roles and create (or refresh) the first-admin invitation. Returns the
    raw invitation token."""
    from app.core.database import platform_session, tenant_session
    from app.core.permissions import TENANT_ADMIN
    from app.models.identity import Invitation, Tenant
    from app.services import access_service as access
    from app.services import auth_service

    def _tenant_kwargs() -> dict[str, object]:
        return {
            "id": tenant_id, "name": company_name, "slug": slug, "status": "active",
            "timezone": DEFAULT_TZ, "currency": DEFAULT_CURRENCY, "locale": DEFAULT_LOCALE,
            "seat_limit": DEFAULT_SEAT_LIMIT,
        }

    # (a) Tenant row in the PLATFORM DB (login + session resolution read tenants here).
    async with platform_session() as db:
        if await db.get(Tenant, tenant_id) is None:
            db.add(Tenant(**_tenant_kwargs()))

    # (b) The tenant's OWN DB: self-row, default roles and the admin invitation (RLS-scoped).
    email = admin_email.strip().lower()
    async with tenant_session(tenant_id) as tdb:
        if await tdb.get(Tenant, tenant_id) is None:
            tdb.add(Tenant(**_tenant_kwargs()))
            await tdb.flush()
        role_ids = await access.seed_default_roles(tdb, tenant_id)
        existing = (
            await tdb.execute(
                select(Invitation).where(
                    Invitation.email == email, Invitation.accepted_at.is_(None)
                )
            )
        ).scalars().first()
        if existing is not None:
            # Safe on retry: a still-pending invitation gets a fresh token + expiry (old link stops working).
            return auth_service.rotate_invitation_token(existing)
        issued = await auth_service.create_invitation(
            tdb,
            tenant_id=tenant_id,
            email=email,
            role_ids=[role_ids[TENANT_ADMIN]],
            invited_by_type="platform",
            invited_by_id=tenant_id,  # self-serve: no operator; attribute the invite to the workspace itself
            name=admin_name,
        )
        return issued.token


async def list_companies() -> list["CompanyAdminRow"]:
    """Every company on the platform, enriched with its plan and admin email — for the operator console.

    One query for the ``Tenant`` rows and one for the per-tenant plans (both platform/control-plane reads);
    the admin email is a best-effort per-tenant lookup (the oldest invitation in the tenant's own DB),
    wrapped so a missing/unreachable tenant DB just yields ``None`` rather than failing the whole list.
    Ordered newest first so freshly provisioned / self-serve workspaces surface at the top.
    """
    from app.core.database import platform_session, tenant_session
    from app.models.identity import Invitation, Tenant
    from app.services import entitlements

    async with platform_session() as db:
        tenants = (
            await db.execute(select(Tenant).order_by(Tenant.created_at.desc(), Tenant.id))
        ).scalars().all()

    # Plans live in the platform DB's ``tenant_entitlements`` (control-plane), reached through the
    # entitlements module's own owner engine; ``ensure_schema`` is idempotent so a fresh install is fine.
    await entitlements.ensure_schema()

    rows: list[CompanyAdminRow] = []
    for t in tenants:
        plan: str | None = None
        admin_email: str | None = None
        if t.status != "deleted":
            try:
                plan = await entitlements.tenant_plan(t.id)
            except Exception:  # never let a plan lookup break the console list
                logger.warning("[company_onboarding] plan lookup failed for tenant {}", t.slug)
            try:
                async with tenant_session(t.id) as tdb:
                    admin_email = (
                        await tdb.execute(
                            select(Invitation.email)
                            .order_by(Invitation.created_at.asc(), Invitation.id)
                            .limit(1)
                        )
                    ).scalar_one_or_none()
            except Exception:  # a tenant DB that is gone / unreachable must not break the console list
                logger.warning("[company_onboarding] admin email lookup failed for tenant {}", t.slug)
        rows.append(
            CompanyAdminRow(
                id=t.id,
                name=t.name,
                slug=t.slug,
                status=t.status,
                created_at=t.created_at,
                plan=plan,
                admin_email=admin_email,
            )
        )
    return rows


async def provision_company(
    slug: str,
    company_name: str,
    admin_email: str,
    admin_name: str,
    *,
    model: str | None = None,
) -> ProvisionResult:
    """Provision a brand-new company workspace across the unified platform and return the admin invite.

    ``model`` selects the Talent OS tenancy path (defaults to ``settings.provisioning_model``):

      * ``"db_per_tenant"`` (legacy, unchanged): dedicated DB ``tenant_<slug>`` copied from the template,
        registered with ``schema_name`` NULL -> the request path connects to it on the public schema.
      * ``"schema_per_app"``: ONE DB ``company_<slug>`` with ONE schema per app. Talent OS lives in the
        ``talentos`` schema (migrated to head), registered with ``schema_name='talentos'``; Croar and
        Feedback IQ are registered as sibling schemas (``croar`` / ``feedbackiq``) in the SAME DB and build
        their own tables there on first access. The company DB also carries three per-app restricted roles
        (``talentos_app`` / ``croar_app`` / ``feedbackiq_app``), each confined to its own schema. Existing
        tenants are untouched; the two models coexist, decided per registry row.

    In the legacy ``db_per_tenant`` model the other apps keep their own dedicated per-tenant databases."""
    slug = slug.strip().lower()
    if not SLUG_RE.fullmatch(slug) or slug in RESERVED_SLUGS:
        raise ValidationFailed(
            "Workspace ID must be 2-40 characters: lowercase letters, digits and hyphens",
            code="invalid_slug",
        )
    if not await _slug_is_free(slug):
        raise ConflictError("This workspace ID is already taken", code="slug_taken")

    s = get_settings()
    model = model or s.provisioning_model
    tenant_id = uuid.uuid4()

    # 2. Talent OS tenant: create + migrate its DB (blocking, in a thread), then register it.
    if model == "schema_per_app":
        talentos_db = s.company_db_name(slug)
        schema_name = s.talentos_schema
        await asyncio.to_thread(_provision_company_schema_db, talentos_db, schema_name)
        await tenant_registry.register_tenant(
            tenant_id, slug, talentos_db, cell="local", schema_version="head",
            status="active", schema_name=schema_name,
        )
    else:
        talentos_db = s.tenant_db_name(slug)
        await asyncio.to_thread(_provision_talentos_db, talentos_db)
        await tenant_registry.register_tenant(
            tenant_id, slug, talentos_db, cell="local", schema_version="head", status="active"
        )

    # 3. Register the other apps (Croar / Feedback IQ). They build their own schema on first access, so no
    #    migrations run here -- only the control-plane row is written.
    if model == "schema_per_app":
        # Company-DB model: all three apps share the ONE company DB. Croar / FIQ get a sibling-schema row
        # pointing at company_<slug> + their schema (the empty schema + their restricted role already exist
        # from the template / role step). No per-app database is created.
        for service, other_schema in OTHER_SERVICE_SCHEMAS.items():
            await _register_service(tenant_id, service, slug, talentos_db, schema_name=other_schema)
    else:
        # Legacy db-per-tenant model: a dedicated empty database per app, registered with schema_name NULL.
        for service, prefix in OTHER_SERVICE_PREFIXES.items():
            other_db = f"{prefix}{slug}"
            await asyncio.to_thread(setup_db.ensure_database, other_db)
            await _register_service(tenant_id, service, slug, other_db)

    # 4. Tenant row + roles + admin invitation, then the default plan (enterprise = every module).
    token = await _seed_and_invite(tenant_id, slug, company_name, admin_email, admin_name)
    from app.services import entitlements

    await entitlements.seed_plans()
    await entitlements.set_tenant_plan(tenant_id, DEFAULT_PLAN)

    logger.info("[company_onboarding] provisioned company slug={} tenant_id={}", slug, tenant_id)
    return ProvisionResult(
        tenant_slug=slug,
        admin_email=admin_email.strip().lower(),
        invite_url=_accept_invite_url(slug, token),
    )
