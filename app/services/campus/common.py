# ruff: noqa: E501, D401
"""Shared helpers of the campus domain (Epic 43): constants, errors, settings, scoped loaders and name lookups.

Data scope: ``campus:read`` / ``campus:manage`` on the drive owner (``campus_drives.owner_id``), ``event:manage`` on
the event owner. Institutions are tenant-wide reference data (the permission alone). A context without a principal
(tasks, public pages) is a system context and skips scope.
"""

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext, get_context
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.permissions import Principal, scope_condition
from app.core.settings import get_settings
from app.models.campus import CampusDrive, CampusSettings, RecruitmentEvent
from app.schemas.campus import CampusSettingsIn, CampusSettingsOut

CAMPUS_READ = "campus:read"
CAMPUS_MANAGE = "campus:manage"
EVENT_MANAGE = "event:manage"
ELIGIBILITY_OVERRIDE = "campus_eligibility:override"

LINK_REGISTRATION = "campus_registration"
LINK_EVENT = "event_checkin"
SUBJECT_DRIVE_INSTITUTION = "drive_institution"
SUBJECT_INVITE = "registration_invite"
SUBJECT_EVENT = "recruitment_event"
SUBJECT_CHECKIN = "event_checkin"

CONSENT_PURPOSE = "campus"
CONSENT_SOURCE_REGISTRATION = "campus_registration"
CONSENT_SOURCE_EVENT = "event_checkin"
SEMANTIC_SOURCE = "campus.registration"
HUMAN_ONLY_ENTITY = "campus_registration"
FILE_OWNER = "campus_registration"
QR_OWNER = "drive_institution"


def now() -> datetime:
    return datetime.now(UTC)


def tenant_of(db: AsyncSession) -> uuid.UUID:
    raw = db.sync_session.info.get("tenant_id")
    if raw is None:  # pragma: no cover - tenant sessions only
        msg = "a tenant session is required"
        raise RuntimeError(msg)
    return uuid.UUID(str(raw))


def ctx(principal: Principal | None = None) -> RequestContext:  # noqa: ARG001
    """The request context (``require`` has already put the principal on it)."""
    return get_context()


def conflict(code: str, message: str, **details: Any) -> ConflictError:
    return ConflictError(message, code=code, details=details or None)


def invalid(code: str, message: str, fields: dict[str, str] | None = None) -> ValidationFailed:
    return ValidationFailed(message, code=code, details={"fields": fields} if fields else None)


def principal_of(c: RequestContext) -> Principal | None:
    p = c.principal
    return p if isinstance(p, Principal) and p.type == "user" else None


def user_id_of(c: RequestContext) -> uuid.UUID | None:
    p = principal_of(c)
    return p.id if p is not None else None


def frontend(path: str) -> str:
    return f"{get_settings().frontend_url.rstrip('/')}{path}"


def registration_url(token: str) -> str:
    return frontend(f"/campus/r/{token}")


def event_url(token: str) -> str:
    return frontend(f"/campus/e/{token}")


# ================================================================ settings
async def settings_row(db: AsyncSession) -> CampusSettings:
    row = await db.get(CampusSettings, tenant_of(db))
    if row is None:
        # concurrent first use (e.g. parallel public registrations) races on the PK: create under a savepoint
        # and fall back to the row the winner committed
        try:
            async with db.begin_nested():
                row = CampusSettings(tenant_id=tenant_of(db))
                db.add(row)
                await db.flush()
        except IntegrityError:
            row = await db.get(CampusSettings, tenant_of(db), populate_existing=True)
            if row is None:  # pragma: no cover - the winner rolled back
                raise
        await db.refresh(row)
    return row


async def settings_out(db: AsyncSession) -> CampusSettingsOut:
    return CampusSettingsOut.model_validate(await settings_row(db))


async def update_settings(db: AsyncSession, data: CampusSettingsIn) -> CampusSettingsOut:
    row = await settings_row(db)
    for k, v in data.model_dump().items():
        setattr(row, k, v)
    await db.flush()
    return CampusSettingsOut.model_validate(row)


# ================================================================ scoped loaders
def _scope_ok(c: RequestContext, permission: str, owner_col: Any) -> Any:
    p = principal_of(c)
    if p is None:
        return None
    return scope_condition(p, permission, owner_col)


async def drive_for(
    c: RequestContext, db: AsyncSession, drive_id: uuid.UUID, *, write: bool = False, lock: bool = False
) -> CampusDrive:
    stmt = select(CampusDrive).where(CampusDrive.id == drive_id)
    cond = _scope_ok(c, CAMPUS_MANAGE if write else CAMPUS_READ, CampusDrive.owner_id)
    if cond is not None:
        stmt = stmt.where(cond)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Campus drive not found")
    return row


def drive_scope(c: RequestContext, permission: str = CAMPUS_READ) -> Any:
    """SQL condition limiting drives to the caller's scope (None = all)."""
    return _scope_ok(c, permission, CampusDrive.owner_id)


async def event_for(c: RequestContext, db: AsyncSession, event_id: uuid.UUID, *, lock: bool = False) -> RecruitmentEvent:
    stmt = select(RecruitmentEvent).where(RecruitmentEvent.id == event_id)
    p = principal_of(c)
    if p is not None:
        perm = EVENT_MANAGE if p.has(EVENT_MANAGE) else CAMPUS_READ
        cond = scope_condition(p, perm, RecruitmentEvent.owner_id)
        if cond is not None:
            stmt = stmt.where(cond)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Recruitment event not found")
    return row


# ================================================================ name lookups
async def user_names(db: AsyncSession, ids: Iterable[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    from app.models.identity import User

    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    rows = await db.execute(select(User.id, User.name).where(User.id.in_(wanted)))
    return {r[0]: r[1] for r in rows.all()}


async def candidate_facts(db: AsyncSession, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, tuple[str | None, str | None]]:
    """``{candidate_id: (full_name, primary email)}`` (erased Candidates keep their tombstone name)."""
    from app.models.candidates import Candidate, CandidateEmail

    wanted = list({i for i in ids if i is not None})
    if not wanted:
        return {}
    out: dict[uuid.UUID, tuple[str | None, str | None]] = {}
    for i in range(0, len(wanted), 5000):
        chunk = wanted[i : i + 5000]
        names = dict((await db.execute(select(Candidate.id, Candidate.full_name).where(Candidate.id.in_(chunk)))).tuples().all())
        emails: dict[uuid.UUID, str] = {}
        for cid, email, primary in (
            await db.execute(
                select(CandidateEmail.candidate_id, CandidateEmail.email, CandidateEmail.is_primary).where(
                    CandidateEmail.candidate_id.in_(chunk)
                )
            )
        ).tuples():
            if primary or cid not in emails:
                emails[cid] = str(email)
        for cid in chunk:
            out[cid] = (names.get(cid), emails.get(cid))
    return out


async def demand_refs(db: AsyncSession, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    from app.models.demands import Demand

    wanted = list({i for i in ids if i is not None})
    if not wanted:
        return {}
    rows = await db.execute(select(Demand.id, Demand.title, Demand.req_id, Demand.status).where(Demand.id.in_(wanted)))
    return {r[0]: {"id": r[0], "title": r[1], "req_id": r[2], "status": r[3]} for r in rows.all()}


async def institution_names(db: AsyncSession, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
    from app.models.campus import Institution

    wanted = list({i for i in ids if i is not None})
    if not wanted:
        return {}
    rows = await db.execute(select(Institution.id, Institution.name).where(Institution.id.in_(wanted)))
    return {r[0]: str(r[1]) for r in rows.all()}
