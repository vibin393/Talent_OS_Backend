# ruff: file-ignore[line-too-long]
"""Shared helpers of the BGV domain (Epic 20): settings, loaders, working days, consent, timeline, documents."""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef, actor_from_ctx
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound
from app.models.bgv import BgvCase, BgvCaseEvent, BgvCheck, BgvSettings

CHECK_LABELS: dict[str, str] = {
    "identity": "Identity",
    "employment": "Employer",
    "education": "Education",
    "address": "Address",
    "reference": "Reference",
    "professional_licence": "Professional licence",
    "criminal_record": "Criminal record",
    "drug_test": "Drug test",
}
# Document request catalogue: key -> (label, kind). Unknown keys are uploads labelled from the key.
DOCUMENTS: dict[str, tuple[str, str]] = {
    "aadhaar": ("Aadhaar (last 4 digits + redacted copy)", "upload"),
    "pan_card": ("PAN card", "upload"),
    "passport": ("Passport", "upload"),
    "photo_id": ("Photo ID", "upload"),
    "employment_history": ("Employment history (with HR contacts)", "declaration"),
    "relieving_letter": ("Relieving / experience letters", "upload"),
    "payslips": ("Recent payslips", "upload"),
    "education_history": ("Education history", "declaration"),
    "degree_certificate": ("Degree certificate", "upload"),
    "marksheets": ("Marksheets", "upload"),
    "address_history": ("Address history", "declaration"),
    "address_proof": ("Address proof", "upload"),
    "professional_licence": ("Professional licence", "upload"),
    "police_clearance": ("Police clearance certificate", "upload"),
}
DECLARATION_KEYS = frozenset(k for k, (_, kind) in DOCUMENTS.items() if kind == "declaration")
UPLOAD_KINDS = ("pdf", "png", "jpeg")
UPLOAD_LIMIT_MB = 10
BGV_PURPOSE = "bgv"
REFERENCE_PURPOSE = "reference_check"


def now() -> datetime:
    return datetime.now(UTC)


def today() -> date:
    return now().date()


def add_working_days(start: date, days: int) -> date:
    """``start`` + ``days`` working days (Monday-Friday)."""
    current = start
    remaining = max(int(days), 0)
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def tenant_of(ctx: RequestContext, db: AsyncSession) -> uuid.UUID:
    if ctx.tenant_id is not None:
        return ctx.tenant_id
    return uuid.UUID(str(db.sync_session.info["tenant_id"]))


def user_id(ctx: RequestContext) -> uuid.UUID | None:
    principal = ctx.principal
    return getattr(principal, "id", None) if getattr(principal, "type", None) == "user" else None


def actor(ctx: RequestContext) -> ActorRef:
    return actor_from_ctx(ctx)


def doc_label(key: str) -> tuple[str, str]:
    return DOCUMENTS.get(key, (key.replace("_", " ").capitalize(), "upload"))


_DEFAULTS = {
    "doc_reminder_every_days": 2,
    "discrepancy_response_working_days": 5,
    "reference_link_days": 7,
    "reference_reminder_days": 2,
    "reuse_clear_case_days": 180,
    "provider_link_days": 21,
}


async def settings_row(db: AsyncSession, tenant_id: uuid.UUID) -> BgvSettings:
    """Return the tenant's settings row, created lazily. External (worker) sessions cannot see the internal-only table:
    they get the defaults (a transient row, never added to the session)."""
    from sqlalchemy.exc import DBAPIError

    row = await db.get(BgvSettings, tenant_id)
    if row is not None:
        return row
    try:
        async with db.begin_nested():
            row = BgvSettings(tenant_id=tenant_id, **_DEFAULTS)
            db.add(row)
            await db.flush()
        return row
    except DBAPIError:
        return BgvSettings(tenant_id=tenant_id, **_DEFAULTS)


def case_not_found() -> NotFound:
    return NotFound("BGV case not found")


async def get_case(db: AsyncSession, case_id: uuid.UUID, *, lock: bool = False) -> BgvCase:
    stmt = select(BgvCase).where(BgvCase.id == case_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one_or_none()
    if row is None:
        raise case_not_found()
    return row


async def get_check(db: AsyncSession, check_id: uuid.UUID, *, lock: bool = False) -> BgvCheck:
    stmt = select(BgvCheck).where(BgvCheck.id == check_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one_or_none()
    if row is None:
        raise NotFound("BGV check not found")
    return row


def record_event(
    db: AsyncSession,
    case: BgvCase,
    kind: str,
    who: ActorRef,
    *,
    from_status: str | None = None,
    to_status: str | None = None,
    **detail: Any,
) -> None:
    db.add(
        BgvCaseEvent(
            id=uuid.uuid4(),
            tenant_id=case.tenant_id,
            case_id=case.id,
            candidate_id=case.candidate_id,
            kind=kind,
            from_status=from_status,
            to_status=to_status,
            actor_kind=who.kind,
            actor_id=who.id,
            detail={k: (str(v) if isinstance(v, uuid.UUID | date) else v) for k, v in detail.items()},
            at=now(),
        )
    )


async def consent_active(db: AsyncSession, case: BgvCase) -> bool:
    """Check consent on **every** start / record call (never cached)."""
    from app.services.privacy.consent import has_active_consent

    if case.consent_id is None or case.consent_withdrawn_at is not None:
        return False
    return await has_active_consent(db, case.candidate_id, BGV_PURPOSE)


async def require_consent(db: AsyncSession, case: BgvCase) -> None:
    if not await consent_active(db, case):
        raise ConflictError(
            "The candidate's background verification consent is missing or withdrawn",
            code="bgv_consent_missing",
        )


async def ensure_notice(db: AsyncSession, tenant_id: uuid.UUID, purpose: str) -> Any:
    """Return the active ``bgv`` / ``reference_check`` notice, seeding it idempotently for tenants created after 207_01."""
    from app.models.privacy import ConsentNotice
    from app.services.privacy.consent import active_notice

    notice = await active_notice(db, tenant_id, purpose)
    if notice is not None:
        return notice
    titles = {
        BGV_PURPOSE: (
            "Background verification",
            "With your consent we verify the information and documents you provide, contacting the named sources "
            "and verification providers. Only the data a check needs is shared. You can withdraw consent at any "
            "time; verification then stops. Evidence is kept for 12 months after the verification decision.",
        ),
        REFERENCE_PURPOSE: (
            "Reference checks",
            "With your consent we contact the referees you name and ask them a short questionnaire about your work. "
            "Their answers are seen only by the hiring team.",
        ),
    }
    title, body = titles[purpose]
    notice = ConsentNotice(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        purpose=purpose,
        locale="en",
        version=1,
        title=title,
        body_md=body,
        active=True,
    )
    db.add(notice)
    await db.flush()
    return notice


async def candidate_names(db: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    from app.models.candidates import Candidate

    if not ids:
        return {}
    rows = await db.execute(select(Candidate.id, Candidate.full_name).where(Candidate.id.in_(ids)))
    return {cid: name for cid, name in rows.all()}  # ruff: ignore[unnecessary-comprehension]


def short_name(full_name: str | None) -> str | None:
    """First name + last initial (what a BGV provider sees)."""
    parts = (full_name or "").split()
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else f"{parts[0]} {parts[-1][0]}."


async def compliance_recipients(db: AsyncSession) -> list[uuid.UUID]:
    from app.services import identity_bridge

    return [u["id"] for u in await identity_bridge.users_with_permission(db, "bgv:manage")]
