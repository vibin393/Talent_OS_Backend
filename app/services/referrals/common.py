# ruff: file-ignore[non-imperative-mood]
"""Shared helpers of the ``referrals`` domain (Epic 42): permissions, errors, the referrer identity, settings,
clock and the data scope of the internal views."""

import dataclasses
import uuid
from datetime import UTC, date, datetime
from typing import Any, Literal

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.fiscal import today_for
from app.core.permissions import Principal, forbidden, scope_condition
from app.models.referrals import ReferralSettings

REF_READ = "referral:read"
REF_MANAGE = "referral:manage"
REF_CONFIGURE = "referral:configure"
REWARD_APPROVE = "referral_reward:approve"

LINK_PURPOSE = "referral_link"  # Secure Link purpose (both channels)
SUBJECT_REFERRAL = "referral"  # upload channel: accept / decline, single use
SUBJECT_SHARE = "referral_share"  # share channel: self-submit, multi use
CONSENT_PURPOSE = "referral"
CONSENT_SOURCE = "referral_link"
APPROVAL_SUBJECT = "referral_reward"
POSTING_SOURCE = "referral_reward"
FILE_OWNER = "referral"
TZ = "Asia/Kolkata"
OPEN_PLACEMENT_STATUSES = ("pre_joining", "joining_confirmed", "active", "offboarding")
ACTIVE_PLACEMENT_STATUSES = ("active", "offboarding")
SHARE_LINK_MAX_USES = 500
MAX_RESUME_MB = 10

ReferrerKind = Literal["user", "worker"]


@dataclasses.dataclass(frozen=True, slots=True)
class Referrer:
    """Who refers: an internal User (``/me/referrals``) or a Worker (Candidate with a Placement)."""

    kind: ReferrerKind
    id: uuid.UUID
    tenant_id: uuid.UUID
    email: str | None = None
    name: str | None = None
    role_keys: frozenset[str] = frozenset()

    @property
    def first_name(self) -> str | None:
        return self.name.split(" ")[0] if self.name else None

    def columns(self) -> dict[str, Any]:
        return {
            "referrer_kind": self.kind,
            "referrer_user_id": self.id if self.kind == "user" else None,
            "referrer_candidate_id": self.id if self.kind == "worker" else None,
        }


def now() -> datetime:
    return datetime.now(UTC)


def today() -> date:
    return today_for(TZ)


def not_found(what: str = "Referral") -> NotFound:
    return NotFound(f"{what} not found")


def conflict(code: str, message: str, **details: Any) -> ConflictError:
    return ConflictError(message, code=code, details=details or None)


def invalid(code: str, message: str, **details: Any) -> ValidationFailed:
    return ValidationFailed(message, code=code, details=details or None)


def tenant_of(db: AsyncSession) -> uuid.UUID:
    raw = db.sync_session.info.get("tenant_id")
    if raw is None:  # pragma: no cover - tenant sessions only
        msg = "a tenant session is required"
        raise RuntimeError(msg)
    return uuid.UUID(str(raw))


def user_principal(ctx: RequestContext) -> Principal | None:
    p = ctx.principal
    return p if isinstance(p, Principal) and p.type == "user" else None


def has(ctx: RequestContext, permission: str) -> bool:
    p = ctx.principal
    return isinstance(p, Principal) and p.permissions.get(permission) is not None


def require(ctx: RequestContext, permission: str) -> None:
    if ctx.principal is None:  # system / task context
        return
    if not has(ctx, permission):
        raise forbidden(permission)


def system_ctx(tenant_id: uuid.UUID) -> RequestContext:
    return RequestContext(tenant_id=tenant_id, actor_kind="system")


def scoped(ctx: RequestContext, stmt: Select[Any], permission: str = REF_READ) -> Select[Any]:
    """Referral data scope: through the Demand's owner / BU; general referrals (no Demand) need tenant scope.
    The statement must already (outer-)join ``Demand``."""
    from app.models.demands import Demand

    p = ctx.principal
    if not isinstance(p, Principal):
        return stmt
    cond = scope_condition(p, permission, Demand.owner_id, Demand.business_unit_id)
    return stmt if cond is None else stmt.where(cond)


async def get_settings_row(db: AsyncSession) -> ReferralSettings:
    """The tenant's settings row (created with the defaults on first use)."""
    tid = tenant_of(db)
    row = await db.get(ReferralSettings, tid)
    if row is None:
        row = ReferralSettings(tenant_id=tid)
        db.add(row)
        await db.flush()
        await db.refresh(row)
    return row


async def user_referrer(db: AsyncSession, principal: Principal) -> Referrer:
    """The internal User behind a ``/me/referrals`` call (active Users only)."""
    from app.models.identity import Role, User

    user = await db.get(User, principal.id)
    if user is None or not user.is_active:
        raise forbidden("referral")
    keys: frozenset[str] = frozenset()
    if principal.role_ids:
        keys = frozenset(
            (await db.execute(select(Role.key).where(Role.id.in_(list(principal.role_ids))))).scalars()
        )
    return Referrer("user", user.id, user.tenant_id, str(user.email), user.name, keys)


async def worker_referrer(db: AsyncSession, tenant_id: uuid.UUID, candidate_id: uuid.UUID) -> Referrer:
    """A Worker referrer: a live Candidate with at least one Placement (else 403 ``not_a_worker``)."""
    from app.core.errors import Forbidden
    from app.models.candidates import Candidate
    from app.models.placements import Placement
    from app.services.candidates import portal_port

    cand = await db.get(Candidate, candidate_id)
    has_placement = (
        await db.execute(select(Placement.id).where(Placement.candidate_id == candidate_id).limit(1))
    ).first()
    if cand is None or cand.merged_into_id is not None or cand.erased_at is not None or has_placement is None:
        raise Forbidden("Referrals are open to workers on an assignment", code="not_a_worker")
    return Referrer(
        "worker", candidate_id, tenant_id, await portal_port.primary_email(db, candidate_id), cand.full_name
    )


async def referrer_names(
    db: AsyncSession, user_ids: set[uuid.UUID], candidate_ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    from app.models.candidates import Candidate
    from app.models.identity import User

    out: dict[uuid.UUID, str] = {}
    if user_ids:
        out.update(
            dict((await db.execute(select(User.id, User.name).where(User.id.in_(user_ids)))).tuples().all())
        )
    if candidate_ids:
        out.update(
            dict(
                (
                    await db.execute(
                        select(Candidate.id, Candidate.full_name).where(Candidate.id.in_(candidate_ids))
                    )
                )
                .tuples()
                .all()
            )
        )
    return out
