"""Shared plumbing of the pipeline service: clock, principal / actor helpers, errors, visibility of
applications (scoped through the demand, AD-11), settings row and the activity adapter."""

import contextlib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from loguru import logger
from sqlalchemy import column, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ConflictError, DomainError, NotFound
from app.core.permissions import Principal, check, forbidden
from app.models.demands import Demand
from app.models.pipeline import Application, PipelineSettings

ActorKind = Literal["user", "candidate", "system", "workflow", "ai", "api_key", "agent", "app"]  # +agent (53), +app (66)
ACTOR_KINDS: tuple[str, ...] = ("user", "candidate", "system", "workflow", "ai", "api_key", "agent", "app")

READ = "application:read"
CREATE = "application:create"
MOVE = "application:move"
REJECT = "application:reject"
SCREEN = "screening:write"
SUBMIT = "submission:create"
SUBMISSION_READ = "submission:read"
FEEDBACK = "submission:update"
CONFIGURE = "pipeline:configure"

DEFAULT_BURDEN: dict[str, int] = {"contract": 18, "contract_to_hire": 18, "permanent": 0, "freelance": 0}
DEFAULT_REJECTION_SUBJECT = "Update on your application"
DEFAULT_REJECTION_BODY = (
    "Hello {first_name},\n\nThank you for your interest in the {job_title} role. After careful consideration "
    "we will not be moving forward with your application at this time.\n\nWe wish you every success.\n\n"
    "{tenant_name}"
)


class ServiceUnavailable(DomainError):  # noqa: N818 - mirrors HTTP 503
    status_code = 503
    code = "service_unavailable"
    default_message = "The service is temporarily unavailable"


def now() -> datetime:
    return datetime.now(UTC)


def principal_of(ctx: RequestContext) -> Principal:
    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.tenant_id is None:
        raise NotFound("Resource not found")
    return principal


# Epic 67: principals whose data scope applies (a delegated App Principal is scoped like its user).
SCOPED_PRINCIPAL_TYPES = frozenset({"user", "app"})


def actor_id(ctx: RequestContext) -> uuid.UUID | None:
    return getattr(ctx.principal, "id", None)


def actor_kind_of(ctx: RequestContext) -> ActorKind:
    if getattr(ctx, "actor_kind", None) == "agent":  # Epic 53: an agent never passes as ``system``
        return "agent"
    kind = getattr(ctx.principal, "type", None)
    if kind == "user":
        return "user"
    if kind == "api_key":
        return "api_key"
    if kind == "app":  # Epic 66 (AD-75): never passes as ``system`` or ``user``
        return "app"
    return "system"


def tenant_of(db: AsyncSession) -> uuid.UUID:
    raw = db.sync_session.info.get("tenant_id")
    if raw is None:
        msg = "pipeline service needs a tenant session"
        raise RuntimeError(msg)
    return uuid.UUID(str(raw))


def conflict(code: str, message: str, **details: Any) -> ConflictError:
    return ConflictError(message, code=code, details=details or None)


@contextlib.contextmanager
def acting_as(db: AsyncSession, actor_type: str, actor: uuid.UUID | None = None) -> Iterator[None]:
    """Attribute audit rows flushed inside the block to ``actor_type`` (the caller flushes inside)."""
    info = db.sync_session.info
    previous = info.get(audit.ACTOR_INFO_KEY)
    audit.set_actor(db, actor_type, actor)
    try:
        yield
    finally:
        if previous is None:
            info.pop(audit.ACTOR_INFO_KEY, None)
        else:
            info[audit.ACTOR_INFO_KEY] = previous


def dec(value: Decimal | int | float | str) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


# ================================================================ savepoints
@contextlib.asynccontextmanager
async def item_savepoint(db: AsyncSession) -> Any:
    """``begin_nested`` that also drops the domain events buffered inside it when it rolls back."""
    from app.core.events import _PENDING_KEY

    info = db.sync_session.info
    mark = len(info.get(_PENDING_KEY, []))
    try:
        async with db.begin_nested():
            yield
    except BaseException:
        pending = info.get(_PENDING_KEY)
        if pending is not None:
            del pending[mark:]
        raise


# ================================================================ settings row
async def settings_row(db: AsyncSession) -> PipelineSettings:
    row = (await db.execute(select(PipelineSettings))).scalar_one_or_none()
    if row is None:
        row = PipelineSettings(tenant_id=tenant_of(db), burden_pct=dict(DEFAULT_BURDEN))
        db.add(row)
        await db.flush()
        await db.refresh(row)
    return row


# ================================================================ visibility (through the demand)
def _demand_condition(principal: Principal, permission: str) -> Any:
    from app.services.demands.common import access_condition

    return access_condition(principal, permission)


async def demand_for(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, permission: str = READ
) -> Demand:
    """Return the demand if the caller holds ``permission`` on it (demand scope)."""
    principal = principal_of(ctx)
    if principal.type not in SCOPED_PRINCIPAL_TYPES:  # Epic 67: delegated App Principals are scoped like users
        demand = await db.get(Demand, demand_id)
        if demand is None:
            raise NotFound("Demand not found")
        return demand
    check(principal, permission)
    stmt = select(Demand).where(Demand.id == demand_id)
    cond = _demand_condition(principal, permission)
    if cond is not None:
        stmt = stmt.where(cond)
    demand = (await db.execute(stmt)).scalar_one_or_none()
    if demand is None:
        # Distinguish "invisible" (404) from "visible but not in this permission's scope" (403).
        read_cond = _demand_condition(principal, READ) if READ in principal.permissions else None
        visible_stmt = select(Demand.id).where(Demand.id == demand_id)
        if READ not in principal.permissions:
            raise NotFound("Demand not found")
        if read_cond is not None:
            visible_stmt = visible_stmt.where(read_cond)
        if (await db.execute(visible_stmt)).first() is None:
            raise NotFound("Demand not found")
        raise forbidden(permission)
    return demand


def visible_applications(principal: Principal, permission: str = READ) -> Any:
    """``select(Application)`` narrowed to applications whose demand is in ``permission``'s scope."""
    stmt = select(Application)
    if principal.type not in SCOPED_PRINCIPAL_TYPES:  # Epic 67: delegated App Principals are scoped like users
        return stmt
    if permission not in principal.permissions:
        return stmt.where(Application.id.is_(None))
    cond = _demand_condition(principal, permission)
    if cond is None:
        return stmt
    return stmt.where(Application.demand_id.in_(select(Demand.id).where(cond)))


async def application_for(
    ctx: RequestContext,
    db: AsyncSession,
    application_id: uuid.UUID,
    permission: str = READ,
    *,
    for_update: bool = False,
) -> Application:
    """Readable application (else 404) on which the caller holds ``permission`` (else 403)."""
    principal = principal_of(ctx)
    if principal.type not in SCOPED_PRINCIPAL_TYPES:  # Epic 67: delegated App Principals are scoped like users
        stmt = select(Application).where(Application.id == application_id)
        if for_update:
            stmt = stmt.with_for_update(of=Application)
        app = (await db.execute(stmt)).scalar_one_or_none()
        if app is None:
            raise NotFound("Application not found")
        return app
    if READ not in principal.permissions:
        raise NotFound("Application not found")
    stmt = visible_applications(principal, READ).where(Application.id == application_id)
    if for_update:
        stmt = stmt.with_for_update(of=Application)
    app = (await db.execute(stmt)).scalar_one_or_none()
    if app is None:
        raise NotFound("Application not found")
    if permission != READ:
        check(principal, permission)
        scoped = visible_applications(principal, permission).where(Application.id == application_id)
        if (await db.execute(scoped.with_only_columns(Application.id))).first() is None:
            raise forbidden(permission)
    return app


# ================================================================ users (read-only lookups)
_users = table("users", column("id"), column("name"), column("email"), column("is_active"))


async def user_emails(db: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = await db.execute(
        select(_users.c.id, _users.c.email).where(_users.c.id.in_(ids), _users.c.is_active.is_(True))
    )
    return {r[0]: str(r[1]) for r in rows.all() if r[1]}


# ================================================================ activity
async def timeline(
    ctx: RequestContext,
    db: AsyncSession,
    entity_type: str,
    entity_id: uuid.UUID,
    body: str,
    data: dict[str, Any] | None = None,
    *,
    kind: str = "system",
) -> None:
    """System (or ``email``) entry on a record's timeline. ``body``/``data`` carry ids and codes only."""
    try:
        from app.services import activity_service
    except ImportError:  # pragma: no cover - Epic 2 absent
        logger.debug("activity service unavailable; pipeline timeline entry skipped")
        return
    try:
        async with db.begin_nested():
            await activity_service.add_system_entry(ctx, db, entity_type, entity_id, body, data, kind=kind)
    except Exception:
        logger.exception("Pipeline timeline entry failed ({}:{})", entity_type, entity_id)
