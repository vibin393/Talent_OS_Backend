"""Candidate privacy self-service (Epic 9 story 9.4, FR-51; DSRs handled under FR-68 by Epic 12).

* Consents: list (``client_name`` only for ``client_submission``), withdraw (``withdrawn_by_kind=candidate``;
  the AI gateway denies from the next call), re-grant ``ai_processing`` / ``marketing`` against the active
  notice. Withdrawing ``data_processing`` answers ``details.suggest_erasure = true``.
* Data export: an ``access`` DSR (Epic 12 ``dsr_requests``, channel ``candidate_portal``) + a
  ``data_export_jobs`` row; one active job at a time (409 ``export_in_progress``), 3 per day. The Celery task
  builds ``data.json`` from the **portal projections only** plus the candidate's own resume files, stores
  ``t/<tid>/exports/<job>.zip``, issues a ``data_export_download`` secure link (3 uses), emails it, marks the
  job ready and completes the DSR.
* Correction / erasure requests: a DSR (due in 30 days), ``DsrRequested`` -> email to the tenant's privacy
  contact. No auto-erasure: Epic 12 fulfils requests.
"""

import contextlib
import io
import json
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, rate_limit, secure_links, storage
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError, NotFound, RateLimited
from app.core.events import emit
from app.core.settings import get_settings
from app.events.portal import DataExportReady, DsrRequested
from app.models.clients import Client
from app.models.portal import ApplicationAnswer, DataExportJob
from app.models.privacy import Consent, ConsentNotice, DsrRequest
from app.schemas.portal import (
    PortalConsent,
    PortalConsentGrantIn,
    PortalConsentWithdrawOut,
    PortalDataExport,
    PortalPrivacyRequest,
    PortalPrivacyRequestIn,
    PortalWithdrawDetails,
)
from app.schemas.privacy import DsrCreate
from app.services.candidates import portal_port as candidates
from app.services.pipeline import portal_port as pipeline
from app.services.portal import projection, self_service
from app.services.portal.common import DATA_EXPORT_DOWNLOAD, CandidatePrincipal, now

ACTIVE_EXPORT_STATUSES = ("queued", "running")
EXPORTS_PER_DAY = 3
EXPORT_MAX_USES = 3
STALE_AFTER = timedelta(hours=24)


# ================================================================ consents
async def _consent_rows(db: AsyncSession, candidate_id: uuid.UUID) -> list[Consent]:
    return list(
        (
            await db.execute(
                select(Consent)
                .where(Consent.candidate_id == candidate_id)
                .order_by(Consent.created_at.desc())
            )
        ).scalars()
    )


async def _consent_out(db: AsyncSession, rows: list[Consent]) -> list[PortalConsent]:
    notice_ids = [r.notice_id for r in rows if r.notice_id]
    client_ids = [r.client_id for r in rows if r.client_id and r.purpose == "client_submission"]
    versions: dict[uuid.UUID, int] = {}
    if notice_ids:
        versions = dict(
            (
                await db.execute(
                    select(ConsentNotice.id, ConsentNotice.version).where(ConsentNotice.id.in_(notice_ids))
                )
            ).tuples().all()
        )
    names: dict[uuid.UUID, str] = {}
    if client_ids:
        names = {
            cid: str(n)
            for cid, n in (
                await db.execute(select(Client.id, Client.name).where(Client.id.in_(client_ids)))
            ).all()
        }
    return [
        projection.consent(
            r,
            names.get(r.client_id) if r.client_id else None,
            versions.get(r.notice_id) if r.notice_id else None,
        )
        for r in rows
    ]


async def list_consents(principal: CandidatePrincipal, db: AsyncSession) -> list[PortalConsent]:
    return await _consent_out(db, await _consent_rows(db, principal.candidate_id))


async def withdraw_consent(
    ctx: RequestContext, principal: CandidatePrincipal, db: AsyncSession, consent_id: uuid.UUID
) -> PortalConsentWithdrawOut:
    from app.services import privacy

    row = await db.get(Consent, consent_id)
    if row is None or row.candidate_id != principal.candidate_id:
        raise NotFound("Consent not found")
    if row.status != "granted":
        raise ConflictError("This consent is not active", code="consent_not_active")
    await privacy.withdraw_consent(
        ctx, db, principal.candidate_id, row.purpose, by_kind="candidate", client_id=row.client_id
    )
    await db.refresh(row)
    out = (await _consent_out(db, [row]))[0]
    return PortalConsentWithdrawOut(
        consent=out, details=PortalWithdrawDetails(suggest_erasure=row.purpose == "data_processing")
    )


async def grant_consent(
    ctx: RequestContext, principal: CandidatePrincipal, db: AsyncSession, data: PortalConsentGrantIn
) -> PortalConsent:
    from app.services import privacy

    notice = await privacy.consent.active_notice(db, principal.tenant_id, data.purpose)
    if notice is None or notice.id != data.notice_id:
        raise ConflictError(
            "The privacy notice changed; please review it again",
            code="notice_outdated",
            details={"purpose": data.purpose, "notice_id": str(notice.id) if notice else None},
        )
    row = await privacy.grant_consent(
        ctx,
        db,
        principal.candidate_id,
        data.purpose,
        source="candidate_portal",
        notice_id=notice.id,
        evidence={"channel": "candidate_portal", "session_id": str(principal.session_id)},
    )
    return (await _consent_out(db, [row]))[0]


# ================================================================ DSR helpers
async def _file_dsr(
    ctx: RequestContext, principal: CandidatePrincipal, db: AsyncSession, kind: str, details: dict[str, Any]
) -> uuid.UUID:
    from app.services import privacy

    out = await privacy.create_dsr(
        ctx,
        db,
        DsrCreate(
            candidate_id=principal.candidate_id, kind=kind, channel="candidate_portal", details=details
        ),
        channel="candidate_portal",
    )
    return out.id


# ================================================================ data export
async def request_export(
    ctx: RequestContext, principal: CandidatePrincipal, db: AsyncSession
) -> PortalDataExport:
    await db.execute(
        select(func.pg_advisory_xact_lock(func.hashtext(f"portal-export:{principal.candidate_id}")))
    )
    active = (
        await db.execute(
            select(DataExportJob.id).where(
                DataExportJob.candidate_id == principal.candidate_id,
                DataExportJob.status.in_(ACTIVE_EXPORT_STATUSES),
            )
        )
    ).first()
    if active is not None:
        raise ConflictError("An export is already being prepared", code="export_in_progress")
    today = int(
        (
            await db.execute(
                select(func.count())
                .select_from(DataExportJob)
                .where(
                    DataExportJob.candidate_id == principal.candidate_id,
                    DataExportJob.requested_at > now() - timedelta(days=1),
                )
            )
        ).scalar_one()
    )
    if today >= EXPORTS_PER_DAY:
        raise RateLimited("You can request at most 3 exports per day", code="rate_limited")
    dsr_id = await _file_dsr(
        ctx, principal, db, "access", {"source": "candidate_portal", "request": "data_export"}
    )
    job = DataExportJob(
        id=uuid.uuid4(),
        tenant_id=principal.tenant_id,
        candidate_id=principal.candidate_id,
        status="queued",
        dsr_id=dsr_id,
        requested_at=now(),
    )
    db.add(job)
    await db.flush()
    await db.refresh(job)
    audit.record("portal.data_export_requested", "data_export_job", job.id, session=db, dsr_id=str(dsr_id))
    emit(db, DsrRequested(tenant_id=principal.tenant_id, dsr_id=dsr_id, kind="access", export_job_id=job.id))
    return projection.data_export(job)


async def list_exports(principal: CandidatePrincipal, db: AsyncSession) -> list[PortalDataExport]:
    rows = (
        await db.execute(
            select(DataExportJob)
            .where(DataExportJob.candidate_id == principal.candidate_id)
            .order_by(DataExportJob.requested_at.desc())
            .limit(20)
        )
    ).scalars()
    return [projection.data_export(r) for r in rows]


async def download_export(
    principal: CandidatePrincipal, db: AsyncSession, job_id: uuid.UUID
) -> tuple[bytes, str]:
    job = await db.get(DataExportJob, job_id)
    if job is None or job.candidate_id != principal.candidate_id:
        raise NotFound("Export not found")
    if job.status != "ready" or not job.storage_key:
        raise ConflictError("The export is not ready", code="export_not_ready")
    try:
        content = await storage.get_storage().get(job.storage_key)
    except FileNotFoundError as exc:
        raise NotFound("Export not found") from exc
    audit.record("portal.data_export_downloaded", "data_export_job", job.id, session=db, channel="portal")
    return content, f"my-data-{job.id}.zip"


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


async def export_data(ctx: RequestContext, principal: CandidatePrincipal, db: AsyncSession) -> dict[str, Any]:
    """``data.json`` of the export: the same portal projections the candidate sees, plus their own answers."""
    applications = await self_service.list_applications(principal, db)
    details = [await self_service.get_application(principal, db, a.id) for a in applications]
    answers = (
        await db.execute(
            select(ApplicationAnswer).where(
                ApplicationAnswer.application_id.in_([a.id for a in applications] or [uuid.uuid4()])
            )
        )
    ).scalars()
    screenings = await pipeline.candidate_screenings(db, principal.candidate_id, None)
    return {
        "generated_at": now(),
        "profile": (await self_service.get_profile(principal, db)).model_dump(mode="json"),
        "consents": [c.model_dump(mode="json") for c in await list_consents(principal, db)],
        "applications": [d.model_dump(mode="json") for d in details],
        "application_answers": [
            {
                "application_id": str(a.application_id),
                "submitted_at": a.submitted_at,
                "answers": projection.answers(a.questions, a.answers),
            }
            for a in answers
        ],
        "interviews": [
            i.model_dump(mode="json")
            for i in await self_service.list_interviews(ctx, principal, db, upcoming=False)
        ],
        "offers": [o.model_dump(mode="json") for o in await self_service.list_offers(ctx, principal, db)],
        "ai_screening_answers": [
            {
                "screening_id": str(s.id),
                "answered_at": s.answered_at,
                "answers": projection.answers(s.questions, s.answers),
            }
            for s in screenings
            if s.answers
        ],
        "privacy_requests": [r.model_dump(mode="json") for r in await list_requests(principal, db)],
    }


async def build_export(tenant_id: uuid.UUID, job_id: uuid.UUID) -> str:
    """Celery body (idempotent: ready / expired jobs are left alone)."""
    from app.core.mailer import EmailMessage, send_email
    from app.services import privacy

    async with tenant_session(tenant_id) as db:
        audit.set_actor(db, "system", None)
        job = await db.get(DataExportJob, job_id, with_for_update=True)
        if job is None or job.status not in ACTIVE_EXPORT_STATUSES:
            return "skipped"
        job.status = "running"
        candidate_id = job.candidate_id
    principal = CandidatePrincipal(candidate_id=candidate_id, tenant_id=tenant_id, session_id=uuid.uuid4())
    ctx = RequestContext(tenant_id=tenant_id, principal=principal.core_principal())
    try:
        async with tenant_session(tenant_id) as db:
            audit.set_actor(db, "system", None)
            data = await export_data(ctx, principal, db)
            files = await candidates.resumes(db, candidate_id)
            recipient = await candidates.primary_email(db, candidate_id)
            site = await projection_site(db, tenant_id)
        buf = io.BytesIO()
        backend = storage.get_storage()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("data.json", json.dumps(data, indent=2, default=_json_default, ensure_ascii=False))
            for index, f in enumerate(files, start=1):
                try:
                    content = await backend.get(f["storage_key"])
                except Exception:
                    logger.warning("Export: resume file {} missing from storage", f["file_id"])
                    continue
                safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in str(f["file_name"] or "file"))
                zf.writestr(f"files/{index:03d}_{safe[:80]}", content)
        key = f"t/{tenant_id}/exports/{job_id}.zip"
        await backend.put(key, buf.getvalue(), "application/zip")
        ttl = timedelta(days=get_settings().data_export_link_days)
        async with tenant_session(tenant_id) as db:
            audit.set_actor(db, "system", None)
            job = await db.get(DataExportJob, job_id, with_for_update=True)
            assert job is not None
            link, token = await secure_links.issue(
                db,
                tenant_id=tenant_id,
                purpose=DATA_EXPORT_DOWNLOAD,
                subject_type="candidate",
                subject_id=candidate_id,
                ttl=ttl,
                max_uses=EXPORT_MAX_USES,
                metadata={"storage_key": key, "job_id": str(job_id), "filename": f"my-data-{job_id}.zip"},
            )
            job.status = "ready"
            job.storage_key = key
            job.secure_link_id = link.id
            job.completed_at = now()
            job.expires_at = link.expires_at
            job.error = None
            if job.dsr_id is not None:
                await privacy.dsr.complete_system(
                    db, job.dsr_id, {"export_job_id": str(job_id), "channel": "candidate_portal"}
                )
            audit.record("portal.data_export_ready", "data_export_job", job_id, session=db)
            emit(db, DataExportReady(tenant_id=tenant_id, job_id=job_id, candidate_id=candidate_id))
    except Exception as exc:
        logger.exception("Portal data export {} failed", job_id)
        async with tenant_session(tenant_id) as db:
            audit.set_actor(db, "system", None)
            job = await db.get(DataExportJob, job_id)
            if job is not None and job.status != "ready":
                job.status = "failed"
                job.error = type(exc).__name__[:200]
        return "failed"
    if recipient:
        brand = site or "Careers"
        await send_email(
            EmailMessage(
                to=recipient,
                subject="Your data export is ready",
                text=(
                    f"Hello,\n\nThe copy of your personal data you requested from {brand} is ready. Download it here:\n"
                    f"{secure_links.link_url(token)}\n\nThe link can be used {EXPORT_MAX_USES} times and expires in "
                    f"{ttl.days} days. You can also download it from the Privacy page of the candidate portal.\n\n{brand}\n"
                ),
                tenant_id=tenant_id,
                kind="data_export_ready",
                from_name=brand,
            )
        )
    return "ready"


async def projection_site(db: AsyncSession, tenant_id: uuid.UUID) -> str | None:
    from app.services.portal import careers

    site = await careers.site_for_tenant(db, tenant_id)
    return site.display_name if site else None


async def public_download(token: str) -> tuple[bytes, str]:
    """``POST /public/links/{token}/download`` for ``data_export_download`` links (consumes one use)."""
    link = await secure_links.resolve(token, (DATA_EXPORT_DOWNLOAD,))
    async with tenant_session(link.tenant_id) as db:
        audit.set_actor(db, "candidate", link.subject_id)
        await secure_links.consume(db, link.id)
        audit.record(
            "portal.data_export_downloaded", "candidate", link.subject_id, session=db, channel="secure_link"
        )
    content = await storage.get_storage().get(str(link.meta.get("storage_key") or ""))
    return content, str(link.meta.get("filename") or "export.zip")


async def expire_exports(tenant_id: uuid.UUID) -> int:
    """Delete export files once their link expired; the job becomes ``expired``."""
    count = 0
    async with tenant_session(tenant_id) as db:
        audit.set_actor(db, "system", None)
        rows = list(
            (
                await db.execute(
                    select(DataExportJob)
                    .where(DataExportJob.status == "ready", DataExportJob.expires_at <= now())
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        for job in rows:
            if job.storage_key:
                with contextlib.suppress(FileNotFoundError):
                    await storage.get_storage().delete(job.storage_key)
            job.status = "expired"
            job.storage_key = None
            count += 1
    return count


async def alert_stale_exports(tenant_id: uuid.UUID) -> int:
    """Jobs not ready 24 h after the request: log an error and put an Activity on the candidate's timeline
    for admins (once per job)."""
    from app.services import activity_service

    count = 0
    async with tenant_session(tenant_id) as db:
        audit.set_actor(db, "system", None)
        rows = list(
            (
                await db.execute(
                    select(DataExportJob).where(
                        DataExportJob.status.in_((*ACTIVE_EXPORT_STATUSES, "failed")),
                        DataExportJob.requested_at <= now() - STALE_AFTER,
                        DataExportJob.alerted_at.is_(None),
                    )
                )
            ).scalars()
        )
        ctx = RequestContext(tenant_id=tenant_id)
        for job in rows:
            logger.error("Portal data export {} not ready after 24 h (status={})", job.id, job.status)
            try:
                async with db.begin_nested():
                    await activity_service.add_system_entry(
                        ctx, db, "candidate", job.candidate_id,
                        "Data export request not ready after 24 hours: check the export job",
                        {"export_job_id": str(job.id), "dsr_id": str(job.dsr_id) if job.dsr_id else None},
                    )  # fmt: skip
            except Exception:
                logger.exception("Could not add the stale-export activity")
            job.alerted_at = now()
            count += 1
    return count


# ================================================================ correction / erasure requests
async def create_request(
    ctx: RequestContext, principal: CandidatePrincipal, db: AsyncSession, data: PortalPrivacyRequestIn
) -> PortalPrivacyRequest:
    await rate_limit.enforce("portal_dsr", str(principal.candidate_id), 10, 86400)
    details: dict[str, Any] = {"text": data.details, "source": "candidate_portal"}
    if data.fields:
        details["fields"] = list(data.fields)
    dsr_id = await _file_dsr(ctx, principal, db, data.kind, details)
    emit(db, DsrRequested(tenant_id=principal.tenant_id, dsr_id=dsr_id, kind=data.kind))
    row = await db.get(DsrRequest, dsr_id)
    assert row is not None
    return projection.privacy_request(row)


async def list_requests(principal: CandidatePrincipal, db: AsyncSession) -> list[PortalPrivacyRequest]:
    rows = (
        await db.execute(
            select(DsrRequest)
            .where(
                DsrRequest.candidate_id == principal.candidate_id, DsrRequest.channel == "candidate_portal"
            )
            .order_by(DsrRequest.received_at.desc())
        )
    ).scalars()
    return [projection.privacy_request(r) for r in rows]


async def notify_privacy_contact(tenant_id: uuid.UUID, dsr_id: uuid.UUID) -> str:
    """Celery body: email the tenant's privacy contact about a portal correction / erasure request (ids and
    the kind only -- the details stay in Talent OS)."""
    from app.core.mailer import EmailMessage, send_email
    from app.services.portal import careers

    async with tenant_session(tenant_id) as db:
        row = await db.get(DsrRequest, dsr_id)
        site = await careers.site_for_tenant(db, tenant_id)
        if row is None or site is None or not site.privacy_contact_email:
            return "skipped"
        human_id, kind, due = row.human_id, row.kind, row.due_at
    await send_email(
        EmailMessage(
            to=site.privacy_contact_email,
            subject=f"New {kind} request {human_id} from the candidate portal",
            text=(
                f"A candidate filed a {kind} request ({human_id}) from the candidate portal.\n"
                f"It is due by {due.date().isoformat()}. Review it under Privacy > Data subject requests in Talent OS.\n"
            ),
            tenant_id=tenant_id,
            kind="dsr_portal_request",
        )
    )
    return "sent"
