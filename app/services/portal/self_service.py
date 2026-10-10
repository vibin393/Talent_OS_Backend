"""Candidate self-service (Epic 9 story 9.4, FR-50): profile, resumes, applications with candidate-facing stage
labels, withdraw, upcoming interviews, offers (read model) and AI screenings.

Every function is scoped to the signed-in candidate (``principal.candidate_id``) inside the candidate's
tenant session: another candidate's record is simply not found (404). Output only through ``projection``.
Offer view / accept / decline endpoints are Epic 8's (``/api/v1/portal/offers``); Epic 7 / 8 read models are
reached through guarded imports and fall back to empty lists when those domains are absent.
"""

import importlib
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.events.portal import ApplicationWithdrawnByCandidate, CandidateProfileUpdated
from app.models.clients import Client
from app.models.demands import Demand, DemandPosting
from app.models.portal import CareerSite
from app.schemas.portal import (
    PortalApplication,
    PortalApplicationDetail,
    PortalInterview,
    PortalOffer,
    PortalProfile,
    PortalProfileUpdate,
    PortalResume,
    PortalScreening,
    PortalScreeningConsentIn,
    PortalScreeningDetail,
    PortalStatus,
    PortalWithdrawIn,
    PortalWithdrawReason,
)
from app.services import files_service
from app.services.candidates import portal_port as candidates
from app.services.pipeline import portal_port as pipeline
from app.services.portal import projection
from app.services.portal.common import CandidatePrincipal, now

PROFILE_FIELD_MAP = {
    "full_name": "full_name",
    "headline": "current_title",
    "notice_period_days": "notice_period_days",
    "willing_to_relocate": "willing_to_relocate",
    "work_authorization": "work_authorization",
}


async def stage_labels(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, Any]:
    site = await db.get(CareerSite, tenant_id)
    return dict(site.candidate_stage_labels or {}) if site is not None else {}


# ================================================================ profile
async def get_profile(principal: CandidatePrincipal, db: AsyncSession) -> PortalProfile:
    return projection.profile(await candidates.profile(db, principal.candidate_id))


async def update_profile(
    ctx: RequestContext, principal: CandidatePrincipal, db: AsyncSession, data: PortalProfileUpdate
) -> PortalProfile:
    provided = data.model_fields_set
    if "emails" in provided:
        raise ValidationFailed(
            "Email addresses change through a correction request",
            code="email_change_via_request",
            details={"fields": {"emails": "Request a correction from the Privacy page"}},
        )
    fields: dict[str, Any] = {}
    for key, target in PROFILE_FIELD_MAP.items():
        if key in provided:
            value = getattr(data, key)
            if key == "full_name" and not value:
                raise ValidationFailed("Name cannot be empty", details={"fields": {"full_name": "Required"}})
            fields[target] = value
    if "location" in provided:
        loc = data.location.model_dump() if data.location is not None else None
        fields["location"] = loc if loc and any(loc.values()) else None
    if "expected_compensation" in provided:
        comp = data.expected_compensation
        fields["expected_ctc_amount"] = Decimal(comp.amount) if comp and comp.amount is not None else None
        fields["expected_ctc_currency"] = comp.currency if comp else None
        fields["expected_ctc_unit"] = comp.unit if comp else None
    if "phones" in provided:
        phones = []
        for i, raw in enumerate(data.phones or []):
            phone = candidates.clean_phone(raw)
            if phone is None:
                raise ValidationFailed(
                    "Invalid phone number",
                    code="invalid_phone",
                    details={"fields": {f"phones.{i}": "Invalid"}},
                )
            phones.append(phone)
        fields["phones"] = phones
    if "links" in provided:
        links = []
        for i, raw in enumerate(data.links or []):
            url = candidates.clean_url(raw)
            if url is None:
                raise ValidationFailed(
                    "Invalid link", code="invalid_link", details={"fields": {f"links.{i}": "Invalid URL"}}
                )
            links.append(url)
        fields["links"] = links
    changed = await candidates.update_profile(ctx, db, principal.candidate_id, fields)
    if changed:
        audit.record(
            "portal.profile_updated", "candidate", principal.candidate_id, session=db, fields=changed
        )
        emit(db, CandidateProfileUpdated(tenant_id=principal.tenant_id, candidate_id=principal.candidate_id))
    return await get_profile(principal, db)


# ================================================================ resumes
async def list_resumes(principal: CandidatePrincipal, db: AsyncSession) -> list[PortalResume]:
    return [projection.resume(r) for r in await candidates.resumes(db, principal.candidate_id)]


async def upload_resume(
    ctx: RequestContext, principal: CandidatePrincipal, db: AsyncSession, data: bytes, filename: str | None
) -> PortalResume:
    from app.services import privacy

    parse = await privacy.has_active_consent(db, principal.candidate_id, "ai_processing")
    rid = await candidates.upload_resume(ctx, db, principal.candidate_id, data, filename, parse=parse)
    audit.record(
        "portal.resume_uploaded", "candidate", principal.candidate_id, session=db, resume_id=str(rid)
    )
    for row in await candidates.resumes(db, principal.candidate_id):
        if row["id"] == rid:
            return projection.resume(row)
    raise NotFound("Resume not found")  # pragma: no cover


async def download_resume(
    principal: CandidatePrincipal, db: AsyncSession, resume_id: uuid.UUID
) -> files_service.Download:
    file = await candidates.resume_file(db, principal.candidate_id, resume_id)
    data = await files_service.read_file(file)
    return files_service.Download(data=data, filename=file.original_name, content_type=file.content_type)


# ================================================================ applications
async def _demand_info(db: AsyncSession, demand_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    if not demand_ids:
        return {}
    rows = (
        await db.execute(
            select(Demand.id, Demand.title, Client.name, DemandPosting.show_client_name)
            .join(Client, Client.id == Demand.client_id)
            .outerjoin(DemandPosting, DemandPosting.demand_id == Demand.id)
            .where(Demand.id.in_(demand_ids))
        )
    ).all()
    return {
        did: {"title": title, "client_name": client, "show_client_name": bool(show)}
        for did, title, client, show in rows
    }


async def list_applications(principal: CandidatePrincipal, db: AsyncSession) -> list[PortalApplication]:
    apps = await pipeline.candidate_applications(db, principal.candidate_id)
    info = await _demand_info(db, list({a["demand_id"] for a in apps}))
    labels = await stage_labels(db, principal.tenant_id)
    return [projection.application(a, info.get(a["demand_id"], {}), labels) for a in apps]


async def get_application(
    principal: CandidatePrincipal, db: AsyncSession, application_id: uuid.UUID
) -> PortalApplicationDetail:
    app = await pipeline.candidate_application(db, principal.candidate_id, application_id)
    info = await _demand_info(db, [app["demand_id"]])
    labels = await stage_labels(db, principal.tenant_id)
    history = await pipeline.stage_history(db, application_id)
    return projection.application_detail(app, info.get(app["demand_id"], {}), labels, history)


async def withdraw_reasons(db: AsyncSession) -> list[PortalWithdrawReason]:
    return [
        PortalWithdrawReason(code=r["code"], label=r["label"]) for r in await pipeline.withdraw_reasons(db)
    ]


async def withdraw(
    ctx: RequestContext,
    principal: CandidatePrincipal,
    db: AsyncSession,
    application_id: uuid.UUID,
    data: PortalWithdrawIn,
) -> PortalApplicationDetail:
    from app.services import pipeline_service

    app = await pipeline.candidate_application(db, principal.candidate_id, application_id)
    if app["terminal"]:
        raise ConflictError("This application is already closed", code="application_closed")
    await pipeline_service.transition_application(
        ctx, db, application_id, "withdrawn", "candidate", data.reason_code, data.note
    )
    audit.record("portal.application_withdrawn", "application", application_id, session=db)
    emit(
        db,
        ApplicationWithdrawnByCandidate(
            tenant_id=principal.tenant_id, application_id=application_id, candidate_id=principal.candidate_id
        ),
    )
    return await get_application(principal, db, application_id)


# ================================================================ interviews (Epic 7, guarded)
def _optional(module: str) -> Any:
    try:
        return importlib.import_module(module)
    except ImportError:
        return None


async def list_interviews(
    ctx: RequestContext, principal: CandidatePrincipal, db: AsyncSession, *, upcoming: bool = True
) -> list[PortalInterview]:
    """Epic 7's ``interviews_service.list_for_candidate`` if present, else the ``interviews`` table read
    model, else ``[]``. Never feedback, scorecards, kits or notes."""
    rows: list[Any] = []
    service = _optional("app.services.interviews_service")
    fn = getattr(service, "list_for_candidate", None) if service is not None else None
    try:
        if fn is not None:
            rows = list(await fn(ctx, db, principal.candidate_id))
        else:
            rows = await _interview_rows(db, principal.candidate_id)
    except Exception:  # an upstream domain in flux must not break the portal
        logger.exception("Portal: interviews unavailable")
        return []
    info = await _demand_info(
        db, [d for d in {_attr(r, "demand_id") for r in rows} if isinstance(d, uuid.UUID)]
    )
    at = now()
    out: list[PortalInterview] = []
    for r in rows:
        starts = _attr(r, "starts_at")
        status = str(_attr(r, "status") or "scheduled")
        if upcoming and (not isinstance(starts, datetime) or starts < at or status != "scheduled"):
            continue
        title = info.get(_attr(r, "demand_id"), {}).get("title")
        out.append(projection.interview(r, title))
    out.sort(key=lambda i: i.starts_at)
    return out


def _attr(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


async def _interview_rows(db: AsyncSession, candidate_id: uuid.UUID) -> list[dict[str, Any]]:
    models = _optional("app.models.interviews")
    model = getattr(models, "Interview", None) if models is not None else None
    if model is None:
        return []
    rows = (await db.execute(select(model).where(model.candidate_id == candidate_id))).scalars()
    return [
        {
            "id": r.id,
            "demand_id": getattr(r, "demand_id", None),
            "round_name": getattr(r, "stage_name", None),
            "starts_at": r.starts_at,
            "ends_at": getattr(r, "ends_at", None),
            "timezone": getattr(r, "timezone", None),
            "mode": getattr(r, "mode", None),
            "location": getattr(r, "location", None),
            "join_url": getattr(r, "meeting_url", None),
            "status": getattr(r, "status", "scheduled"),
        }
        for r in rows
    ]


# ================================================================ offers (Epic 8, guarded read model)
async def list_offers(
    ctx: RequestContext,  # noqa: ARG001 - portal service signature
    principal: CandidatePrincipal,
    db: AsyncSession,  # noqa: ARG001 - portal service signature
) -> list[PortalOffer]:
    """Epic 8 ``offers_service.list_for_candidate(tenant_id, candidate_id)`` if present (used by the export);
    the offer endpoints themselves are Epic 8's ``/portal/offers`` routes."""
    service = _optional("app.services.offers_service")
    fn = getattr(service, "list_for_candidate", None) if service is not None else None
    if fn is None:
        return []
    try:
        rows = list(await fn(principal.tenant_id, principal.candidate_id))
    except Exception:
        logger.exception("Portal: offers unavailable")
        return []
    out: list[PortalOffer] = []
    for r in rows:
        try:
            out.append(projection.offer(r))
        except Exception:  # an unexpected upstream shape: skip rather than leak
            logger.warning("Portal: offer row skipped (unexpected shape)")
    return out


# ================================================================ AI screenings (Epic 6)
async def _screening_title(db: AsyncSession, application_id: uuid.UUID) -> str:
    demand_id = await pipeline.application_demand(db, application_id)
    info = await _demand_info(db, [demand_id] if demand_id else [])
    return str(info.get(demand_id, {}).get("title") or "Screening") if demand_id else "Screening"


async def list_screenings(principal: CandidatePrincipal, db: AsyncSession) -> list[PortalScreening]:
    rows = await pipeline.candidate_screenings(db, principal.candidate_id, ("sent",))
    return [projection.screening(r, await _screening_title(db, r.application_id)) for r in rows]


async def get_screening(
    principal: CandidatePrincipal, db: AsyncSession, screening_id: uuid.UUID
) -> PortalScreeningDetail:
    from app.services import privacy

    row = await pipeline.candidate_screening(db, principal.candidate_id, screening_id)
    notice = await privacy.consent.active_notice(db, principal.tenant_id, "ai_processing")
    consent_required = not await privacy.has_active_consent(db, principal.candidate_id, "ai_processing")
    return projection.screening_detail(
        row, await _screening_title(db, row.application_id), consent_required, notice
    )


def _link_of(row: Any) -> uuid.UUID:
    if row.status != "sent" or row.secure_link_id is None:
        raise ConflictError("This screening is no longer open", code="screening_not_open")
    return row.secure_link_id


async def screening_consent(
    ctx: RequestContext,
    principal: CandidatePrincipal,
    db: AsyncSession,
    screening_id: uuid.UUID,
    data: PortalScreeningConsentIn,
) -> PortalStatus:
    from app.services.pipeline import screening as screening_service

    row = await pipeline.candidate_screening(db, principal.candidate_id, screening_id, for_update=True)
    link_id = _link_of(row)
    evidence = {"channel": "candidate_portal", "session_id": str(principal.session_id)}
    status = await screening_service.public_consent(db, row, link_id, data.granted, data.notice_id, evidence)
    del ctx
    return PortalStatus(status=status)


async def screening_submit(
    ctx: RequestContext,
    principal: CandidatePrincipal,
    db: AsyncSession,
    screening_id: uuid.UUID,
    answers: dict[str, Any],
) -> PortalStatus:
    """Epic 6 ``public_submit``: validates the answers and consumes (closes) the open secure link."""
    from app.services.pipeline import screening as screening_service

    row = await pipeline.candidate_screening(db, principal.candidate_id, screening_id, for_update=True)
    link_id = _link_of(row)
    status = await screening_service.public_submit(db, row, link_id, answers)
    del ctx
    return PortalStatus(status=status)
