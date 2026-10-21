"""Self-serve company onboarding for the unified platform.

``provision_company`` is the one reusable entry point the public signup endpoint calls. It:

  1. validates the slug (``^[a-z0-9-]{2,40}$``) and checks it is free across every app in the control
     registry (``tenant_db_map``);
  2. provisions the Talent OS tenant — creates ``tenant_<slug>``, migrates it to head and registers it
     in the control plane for ``service=talentos`` (the core of ``scripts/provision_tenant.py``, factored
     here so the endpoint never shells out);
  3. creates + registers the EMPTY per-tenant databases for the other apps (``croar_tenant_<slug>`` and
     ``feedbackiq_tenant_<slug>``) — those apps build their own schema on first access, so only the
     database + the ``tenant_db_map`` row are made here;
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

from app.core import tenant_registry
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

# The other apps' silo DB naming (service -> DB-name prefix). Talent OS uses settings.tenant_db_name.
OTHER_SERVICE_PREFIXES: dict[str, str] = {
    "croar": "croar_tenant_",
    "feedbackiq": "feedbackiq_tenant_",
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
    return f"http://{slug}.talentos.localhost/accept-invite?token={token}"


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


async def _register_service(tenant_id: uuid.UUID, service: str, slug: str, db_name: str) -> None:
    """Upsert the control-registry row for ``service`` (idempotent on (tenant_id, service))."""
    async with tenant_registry.control_engine().begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO tenant_db_map (tenant_id, service, slug, db_name, cell, schema_version, status)
                VALUES (:tid, :svc, :slug, :db, 'local', NULL, 'active')
                ON CONFLICT (tenant_id, service) DO UPDATE SET
                    slug = EXCLUDED.slug, db_name = EXCLUDED.db_name,
                    status = EXCLUDED.status, updated_at = now()
                """
            ),
            {"tid": str(tenant_id), "svc": service, "slug": slug, "db": db_name},
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
    slug: str, company_name: str, admin_email: str, admin_name: str
) -> ProvisionResult:
    """Provision a brand-new company workspace across the unified platform and return the admin invite."""
    slug = slug.strip().lower()
    if not SLUG_RE.fullmatch(slug) or slug in RESERVED_SLUGS:
        raise ValidationFailed(
            "Workspace ID must be 2-40 characters: lowercase letters, digits and hyphens",
            code="invalid_slug",
        )
    if not await _slug_is_free(slug):
        raise ConflictError("This workspace ID is already taken", code="slug_taken")

    s = get_settings()
    tenant_id = uuid.uuid4()

    # 2. Talent OS tenant: create + migrate its DB (blocking, in a thread), then register it.
    talentos_db = s.tenant_db_name(slug)
    await asyncio.to_thread(_provision_talentos_db, talentos_db)
    await tenant_registry.register_tenant(
        tenant_id, slug, talentos_db, cell="local", schema_version="head", status="active"
    )

    # 3. Empty per-tenant DBs for the other apps (they build their own schema on first access).
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
