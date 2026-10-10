"""Cross-domain read helpers shared by Epic 10 (comms, tasks, notifications, workflow).

* **Access** -- ``can_read(ctx, db, entity_type, entity_id)`` asks the owning service through the activity
  entity registry (``activity_service.register_entity``: clients, contacts, candidates, demands, and later
  applications). ``principal_for_user`` builds a user's principal so fan-out can check what each recipient
  may see.
* **Directory** -- user / tenant display facts.
* **Addresses and merge data** -- recipient email + names for candidates and contacts, and the values of the
  merge-field groups (``candidate``, ``contact``, ``client``, ``demand``, ``sender``, ``tenant``, ``task``,
  ``interview``, ``offer``, ``link``).

Other domains' tables are read through lightweight ``sqlalchemy.table()`` projections (like
``core.permissions`` does for scoping): read-only, RLS-scoped, no model imports.
"""

import contextlib
import dataclasses
import importlib
import uuid
from collections.abc import Iterable, Iterator, Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Boolean, DateTime, String, Uuid, column, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.permissions import Principal

_users = table(
    "users",
    column("id", Uuid()),
    column("name", String()),
    column("email", String()),
    column("is_active", Boolean()),
    column("business_unit_id", Uuid()),
)
_tenants = table("tenants", column("id", Uuid()), column("name", String()), column("timezone", String()))
_candidates = table(
    "candidates", column("id", Uuid()), column("full_name", String()), column("owner_id", Uuid())
)
_candidate_emails = table(
    "candidate_emails",
    column("candidate_id", Uuid()),
    column("email", String()),
    column("is_primary", Boolean()),
    column("created_at", DateTime()),
)
_contacts = table(
    "contacts",
    column("id", Uuid()),
    column("client_id", Uuid()),
    column("name", String()),
    column("email", String()),
)
_clients = table("clients", column("id", Uuid()), column("name", String()), column("owner_id", Uuid()))
_demands = table(
    "demands",
    column("id", Uuid()),
    column("title", String()),
    column("req_id", String()),
    column("location", String()),
    column("client_id", Uuid()),
    column("owner_id", Uuid()),
)
_tasks = table(
    "tasks", column("id", Uuid()), column("title", String()), column("due_at", DateTime(timezone=True))
)

LINK_PATHS: dict[str, str] = {
    "client": "/clients/{id}",
    "contact": "/contacts/{id}",
    "demand": "/demands/{id}",
    "candidate": "/candidates/{id}",
    "application": "/applications/{id}",
    "submission": "/submissions/{id}",
    "task": "/tasks?task={id}",
    "email_message": "/emails/{id}",
    "approval_request": "/approvals/{id}",
    "workflow_rule": "/admin/workflows/{id}",
    "workflow_execution": "/admin/workflows/executions/{id}",
    "import_batch": "/candidates/imports/{id}",
    "interview": "/interviews/{id}",
    "offer": "/offers/{id}",
    "export": "/exports?export={id}",
}


def link_path(entity_type: str | None, entity_id: uuid.UUID | str | None) -> str | None:
    if not entity_type or entity_id is None:
        return None
    pattern = LINK_PATHS.get(entity_type)
    return pattern.format(id=entity_id) if pattern else None


def first_name(full_name: str | None) -> str | None:
    if not full_name:
        return None
    return full_name.strip().split()[0] if full_name.strip() else None


def optional_service(*module_paths: str) -> Any | None:
    """Import the first available module of ``module_paths`` (a service of an epic that may not exist yet)."""
    for path in module_paths:
        try:
            return importlib.import_module(path)
        except ImportError:
            continue
    return None


# ================================================================ access
_ACCESS_MODULES = (
    "app.services.clients_service",
    "app.services.candidates",
    "app.services.demands",
    "app.services.pipeline_service",
)
_access_loaded = False


def _load_access_checkers() -> None:
    """Import the services that register access checkers (a Celery worker may not have imported them)."""
    global _access_loaded  # process-wide, idempotent
    if _access_loaded:
        return
    for path in _ACCESS_MODULES:
        optional_service(path)
    _access_loaded = True


def is_access_checked(entity_type: str) -> bool:
    """Whether a domain registered an access checker for ``entity_type``."""
    from app.services import activity_service

    _load_access_checkers()

    return entity_type in set(activity_service.registered_entities().values()) and entity_type in getattr(
        activity_service, "_checkers", {}
    )


async def can_read(ctx: RequestContext, db: AsyncSession, entity_type: str, entity_id: uuid.UUID) -> bool:
    """True when the context's principal may see the record (unregistered entity types: False)."""  # noqa: D401
    from app.services import activity_service

    _load_access_checkers()
    try:
        await activity_service.require_entity(ctx, db, entity_type, entity_id)
    except NotFound:
        return False
    return True


async def require_readable(
    ctx: RequestContext, db: AsyncSession, entity_type: str, entity_id: uuid.UUID
) -> None:
    if not await can_read(ctx, db, entity_type, entity_id):
        raise NotFound("Resource not found")


async def principal_for_user(db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> Principal | None:
    """The user's principal (roles, teams, BU resolved) or None when the user is missing or inactive."""  # noqa: D401
    from app.services import access_service as access

    row = (
        await db.execute(select(_users.c.is_active, _users.c.business_unit_id).where(_users.c.id == user_id))
    ).first()
    if row is None or not row.is_active:
        return None
    user_access = await access.load_access(db, user_id)
    return access.user_principal(
        user_id=user_id,
        tenant_id=tenant_id,
        session_id=None,
        business_unit_id=row.business_unit_id,
        access=user_access,
    )


async def user_can_read(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    entity_type: str | None,
    entity_id: uuid.UUID | None,
) -> bool:
    """Recipient check for fan-out: unregistered entity types (no access checker) are readable."""
    if not entity_type or entity_id is None or not is_access_checked(entity_type):
        return True
    principal = await principal_for_user(db, tenant_id, user_id)
    if principal is None:
        return False
    return await can_read(
        RequestContext(tenant_id=tenant_id, principal=principal), db, entity_type, entity_id
    )


# ================================================================ directory
@dataclasses.dataclass(frozen=True, slots=True)
class UserInfo:
    id: uuid.UUID
    name: str
    email: str
    is_active: bool


async def users(db: AsyncSession, user_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, UserInfo]:
    ids = {u for u in user_ids if u is not None}
    if not ids:
        return {}
    rows = await db.execute(
        select(_users.c.id, _users.c.name, _users.c.email, _users.c.is_active).where(_users.c.id.in_(ids))
    )
    return {r.id: UserInfo(r.id, r.name, str(r.email), bool(r.is_active)) for r in rows}


async def user(db: AsyncSession, user_id: uuid.UUID | None) -> UserInfo | None:
    if user_id is None:
        return None
    return (await users(db, [user_id])).get(user_id)


_roles = table("roles", column("id", Uuid()), column("key", String()))
_user_roles = table("user_roles", column("user_id", Uuid()), column("role_id", Uuid()))


async def role_user_ids(db: AsyncSession, role_key: str) -> set[uuid.UUID]:
    """Active users holding the tenant role with ``role_key``."""
    rows = await db.execute(
        select(_user_roles.c.user_id)
        .join(_roles, _roles.c.id == _user_roles.c.role_id)
        .join(_users, _users.c.id == _user_roles.c.user_id)
        .where(_roles.c.key == role_key, _users.c.is_active.is_(True))
    )
    return set(rows.scalars())


async def active_user_ids(db: AsyncSession, user_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    return {u.id for u in (await users(db, user_ids)).values() if u.is_active}


@dataclasses.dataclass(frozen=True, slots=True)
class TenantInfo:
    id: uuid.UUID
    name: str
    timezone: str

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except (KeyError, ValueError):
            return ZoneInfo("UTC")


async def tenant(db: AsyncSession, tenant_id: uuid.UUID) -> TenantInfo:
    row = (
        await db.execute(select(_tenants.c.name, _tenants.c.timezone).where(_tenants.c.id == tenant_id))
    ).first()
    if row is None:
        return TenantInfo(tenant_id, "Talent OS", "UTC")
    return TenantInfo(tenant_id, row.name, row.timezone or "UTC")


@contextlib.contextmanager
def acting_as(db: AsyncSession, actor_type: str, actor_id: uuid.UUID | None = None) -> Iterator[None]:
    """Attribute audit rows flushed inside the block to ``actor_type`` (``system``, ``workflow``, ...)."""
    info = db.sync_session.info
    previous = info.get(audit.ACTOR_INFO_KEY)
    audit.set_actor(db, actor_type, actor_id)
    try:
        yield
    finally:
        if previous is None:
            info.pop(audit.ACTOR_INFO_KEY, None)
        else:
            info[audit.ACTOR_INFO_KEY] = previous


def session_tenant(db: AsyncSession) -> uuid.UUID:
    raw = db.sync_session.info.get("tenant_id")
    if raw is None:
        msg = "a tenant session is required"
        raise RuntimeError(msg)
    return uuid.UUID(str(raw))


# ================================================================ recipients
@dataclasses.dataclass(frozen=True, slots=True)
class Addressee:
    entity_type: str
    entity_id: uuid.UUID
    address: str | None
    full_name: str | None


async def addressee(db: AsyncSession, entity_type: str, entity_id: uuid.UUID) -> Addressee | None:
    """Email address + name of a candidate (primary email) or contact; None when the record is missing."""
    if entity_type == "candidate":
        name = (
            await db.execute(select(_candidates.c.full_name).where(_candidates.c.id == entity_id))
        ).scalar_one_or_none()
        if name is None:
            return None
        email = (
            await db.execute(
                select(_candidate_emails.c.email)
                .where(_candidate_emails.c.candidate_id == entity_id)
                .order_by(_candidate_emails.c.is_primary.desc(), _candidate_emails.c.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
        return Addressee("candidate", entity_id, str(email).lower() if email else None, name)
    if entity_type == "contact":
        row = (
            await db.execute(select(_contacts.c.name, _contacts.c.email).where(_contacts.c.id == entity_id))
        ).first()
        if row is None:
            return None
        return Addressee("contact", entity_id, str(row.email).lower() if row.email else None, row.name)
    return None


async def primary_contact_id(db: AsyncSession, client_id: uuid.UUID) -> uuid.UUID | None:
    return (
        await db.execute(
            select(_contacts.c.id)
            .where(_contacts.c.client_id == client_id, _contacts.c.email.is_not(None))
            .limit(1)
        )
    ).scalar_one_or_none()


# ================================================================ merge data
def format_local(value: datetime | None, tz: ZoneInfo) -> str | None:
    if value is None:
        return None
    return value.astimezone(tz).strftime("%a, %d %b %Y, %H:%M")


async def merge_data(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    refs: Mapping[str, Any],
    *,
    sender_user_id: uuid.UUID | None = None,
    recipient: Addressee | None = None,
) -> dict[str, dict[str, str | None]]:
    """Values of every merge group reachable from ``refs`` (ids: ``candidate_id``, ``contact_id``,
    ``client_id``, ``demand_id``, ``task_id``, ``interview_id``, ``offer_id``; ``link_url``). The recipient,
    when given, fills the ``candidate`` / ``contact`` group. Missing records simply leave groups empty."""
    info = await tenant(db, tenant_id)
    data: dict[str, dict[str, str | None]] = {"tenant": {"name": info.name}}

    sender = await user(db, sender_user_id)
    if sender is not None:
        data["sender"] = {"name": sender.name, "email": sender.email, "title": None, "signature": sender.name}

    def _uuid(key: str) -> uuid.UUID | None:
        raw = refs.get(key)
        try:
            return uuid.UUID(str(raw)) if raw else None
        except ValueError:
            return None

    candidate_id = _uuid("candidate_id")
    contact_id = _uuid("contact_id")
    if recipient is not None and recipient.entity_type == "candidate":
        candidate_id = recipient.entity_id
    if recipient is not None and recipient.entity_type == "contact":
        contact_id = recipient.entity_id
    if candidate_id is not None and (who := await addressee(db, "candidate", candidate_id)) is not None:
        data["candidate"] = {
            "first_name": first_name(who.full_name),
            "full_name": who.full_name,
            "email": who.address,
        }
    if contact_id is not None and (who := await addressee(db, "contact", contact_id)) is not None:
        data["contact"] = {"first_name": first_name(who.full_name), "full_name": who.full_name}

    client_id = _uuid("client_id")
    if (demand_id := _uuid("demand_id")) is not None:
        row = (
            await db.execute(
                select(_demands.c.title, _demands.c.req_id, _demands.c.location, _demands.c.client_id).where(
                    _demands.c.id == demand_id
                )
            )
        ).first()
        if row is not None:
            data["demand"] = {"title": row.title, "human_id": row.req_id, "location": row.location}
            client_id = client_id or row.client_id
    if client_id is not None:
        name = (
            await db.execute(select(_clients.c.name).where(_clients.c.id == client_id))
        ).scalar_one_or_none()
        if name is not None:
            data["client"] = {"name": str(name)}
    if (task_id := _uuid("task_id")) is not None:
        row = (
            await db.execute(select(_tasks.c.title, _tasks.c.due_at).where(_tasks.c.id == task_id))
        ).first()
        if row is not None:
            data["task"] = {"title": row.title, "due_local": format_local(row.due_at, info.tz)}
    if refs.get("link_url"):
        data["link"] = {"url": str(refs["link_url"])}
    if (interview_id := _uuid("interview_id")) is not None:
        service = optional_service("app.services.interviews_service")
        fn = getattr(service, "merge_fields_for", None)
        if fn is not None:
            data["interview"] = dict(await fn(db, interview_id))
    if (offer_id := _uuid("offer_id")) is not None:
        service = optional_service("app.services.offers_service")
        fn = getattr(service, "merge_fields_for", None)
        if fn is not None:
            data["offer"] = dict(await fn(db, offer_id))
    return data


async def entity_label(db: AsyncSession, entity_type: str | None, entity_id: uuid.UUID | None) -> str | None:
    """Short display label of a record (``REQ-0007 Senior Data Engineer``, a candidate's name, ...)."""
    if not entity_type or entity_id is None:
        return None
    if entity_type == "demand":
        row = (
            await db.execute(select(_demands.c.req_id, _demands.c.title).where(_demands.c.id == entity_id))
        ).first()
        return f"{row.req_id} {row.title}" if row is not None else None
    if entity_type == "candidate":
        return (
            await db.execute(select(_candidates.c.full_name).where(_candidates.c.id == entity_id))
        ).scalar_one_or_none()
    if entity_type == "client":
        return (
            await db.execute(select(_clients.c.name).where(_clients.c.id == entity_id))
        ).scalar_one_or_none()
    if entity_type == "contact":
        return (
            await db.execute(select(_contacts.c.name).where(_contacts.c.id == entity_id))
        ).scalar_one_or_none()
    if entity_type == "task":
        return (await db.execute(select(_tasks.c.title).where(_tasks.c.id == entity_id))).scalar_one_or_none()
    return None


async def entity_owner_id(db: AsyncSession, entity_type: str, entity_id: uuid.UUID) -> uuid.UUID | None:
    projections = {"candidate": _candidates, "client": _clients, "demand": _demands}
    tbl = projections.get(entity_type)
    if tbl is None:
        return None
    return (await db.execute(select(tbl.c.owner_id).where(tbl.c.id == entity_id))).scalar_one_or_none()
