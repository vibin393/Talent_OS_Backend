"""Vendor Portal content (Epic 17 A3/A5/A7): the only module ``router/vendor_portal_vms.py`` calls.

* Every function runs in the vendor's org-scoped session (``VendorDBSessionDep``: ``app.org_kind='vendor'``)
  and
  reads only org-RLS tables (``vendors``, ``vendor_*``, ``placements``, statements, granted documents, AD-26);
  another vendor's (or tenant's) id is simply invisible -> 404.
* Writes need vendor status ``active`` (409 ``vendor_not_active``; ``onboarding`` vendors are read-only) and
  the
  Epic 14 portal permission (403 FR-82 envelope, ``ask=account_manager``).
* Duplicate checks run as a system read in an internal session (never exposed to the vendor).
"""

import uuid
from collections import Counter
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import Forbidden, NotFound
from app.core.events import emit
from app.core.permissions import Principal
from app.core.sequences import next_human_id
from app.events.vendors import VendorSubmissionReceived
from app.models.documents import Document
from app.models.files import StoredFile
from app.models.placements import Placement
from app.models.vendor_statements import VendorPayableStatement
from app.models.vendors import (
    Vendor,
    VendorDocumentUpload,
    VendorRelease,
    VendorSubmission,
    VendorSubmissionEvent,
)
from app.schemas.vendors_portal import (
    PortalDashboardOut,
    PortalDocumentOut,
    PortalLabelCount,
    PortalPlacementOut,
    PortalReleaseOut,
    PortalScorecardOut,
    PortalStatementOut,
    PortalSubmissionIn,
    PortalSubmissionOut,
    PortalTimelineItem,
    PortalUpcomingInterview,
)
from app.services import files_service
from app.services.vendors import releases as releases_svc
from app.services.vendors import submissions as subs_svc
from app.services.vendors.common import ATTESTATION_TEXT, CATEGORY_LABELS, conflict, invalid, now
from app.services.vendors.records import doc_status
from app.services.vendors.work import VendorWork

RELEASES_VIEW = "releases:view"
SUBMISSIONS_CREATE = "submissions:create"
PLACEMENTS_VIEW = "placements:view"
DOCUMENTS_MANAGE = "documents:manage"
STATEMENTS_VIEW = "statements:view"
RESUME_KINDS = ("pdf", "docx")
LOCKED_PROGRESS = frozenset({"submitted", "interviewing", "selected", "offered", "hired", "placement"})


# ================================================================ access rule
def require_perm(principal: Principal, key: str) -> None:
    if key not in principal.portal_permissions:
        raise Forbidden(
            "You do not have access to this area",
            code="portal_permission_required",
            details={"permission": key},
            action=key,
            required_permission=key,
            ask="account_manager",
        )


def require_any_perm(principal: Principal, *keys: str) -> None:
    if not any(k in principal.portal_permissions for k in keys):
        require_perm(principal, keys[0])


async def _vendor(db: AsyncSession, principal: Principal) -> Vendor:
    v = (await db.execute(select(Vendor).where(Vendor.id == principal.org_id))).scalar_one_or_none()
    if v is None:
        raise NotFound("Vendor not found")
    return v


async def assert_can_operate(db: AsyncSession, principal: Principal, action: str) -> Vendor:
    """Write actions need an ``active`` vendor (409 ``vendor_not_active``); reads are fine while
    onboarding."""
    v = await _vendor(db, principal)
    if v.status != "active":
        raise conflict(
            "vendor_not_active",
            "Your organisation is not active yet; this action is unavailable",
            action=action,
        )
    return v


def _actor(principal: Principal) -> ActorRef:
    return ActorRef("vendor_user", principal.id)


# ================================================================ releases
def _release_out(r: VendorRelease, used: int) -> PortalReleaseOut:
    remaining = max(r.submission_limit - used, 0) if r.submission_limit is not None else None
    return PortalReleaseOut(
        id=r.id,
        human_id=r.human_id,
        status=r.status,
        title=r.title,
        positions=r.positions,
        skills=list(r.skills or []),
        location=r.location,
        employment_type=r.employment_type,
        experience_range=r.experience_range,
        jd_public_text=r.jd_public_text,
        client_name=r.client_display_name,  # snapshot: "Confidential client" unless revealed
        max_vendor_rate=r.max_vendor_rate,
        rate_unit=r.rate_unit,
        currency=r.currency,
        submission_limit=r.submission_limit,
        submissions_used=used,
        remaining_slots=remaining,
        deadline_at=r.deadline_at,
        instructions=r.instructions,
        released_at=r.released_at,
    )


async def _used(db: AsyncSession, release_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    return await releases_svc.counts(db, release_ids)


async def list_releases(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    principal: Principal,
) -> list[PortalReleaseOut]:
    require_any_perm(principal, RELEASES_VIEW, SUBMISSIONS_CREATE)
    rows = list(
        (
            await db.execute(
                select(VendorRelease).order_by(VendorRelease.released_at.desc(), VendorRelease.id)
            )
        ).scalars()
    )
    used = await _used(db, [r.id for r in rows])
    return [_release_out(r, used.get(r.id, 0)) for r in rows]


async def _release(db: AsyncSession, release_id: uuid.UUID, *, lock: bool = False) -> VendorRelease:
    stmt = select(VendorRelease).where(VendorRelease.id == release_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Release not found")
    return row


async def get_release(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    principal: Principal,
    release_id: uuid.UUID,
) -> PortalReleaseOut:
    require_any_perm(principal, RELEASES_VIEW, SUBMISSIONS_CREATE)
    r = await _release(db, release_id)
    used = await _used(db, [r.id])
    return _release_out(r, used.get(r.id, 0))


# ================================================================ submissions
async def submit(
    ctx: RequestContext,
    db: AsyncSession,
    principal: Principal,
    release_id: uuid.UUID,
    data: PortalSubmissionIn,
    resume: bytes | None,
    filename: str | None,
) -> PortalSubmissionOut:
    from app.services.candidates.common import normalize_email, normalize_phone
    from app.services.vendors import review

    # Visibility first: another vendor's / tenant's release is a 404 whatever the caller's permissions.
    release = await _release(db, release_id, lock=True)
    require_perm(principal, SUBMISSIONS_CREATE)
    await assert_can_operate(db, principal, "submit")
    if release.status != "active":
        raise conflict("release_closed", "This release no longer accepts submissions")
    if release.deadline_at is not None and now() > release.deadline_at:
        raise conflict("deadline_passed", "The submission deadline has passed")
    currency = (data.currency or release.currency).upper()
    unit = data.rate_unit or release.rate_unit
    if currency != release.currency or (release.rate_unit and unit != release.rate_unit):
        raise invalid(
            "rate_unit_mismatch",
            "Quote the rate in the release's currency and unit",
            currency=release.currency,
            rate_unit=release.rate_unit,
        )
    if release.max_vendor_rate is not None and data.vendor_rate > release.max_vendor_rate:
        raise invalid(
            "vendor_rate_above_max",
            "The rate is above the maximum for this release",
            max_vendor_rate=str(release.max_vendor_rate),
        )
    if not data.attestation:
        raise invalid("attestation_required", "Confirm the attestation to submit")
    if release.submission_limit is not None:
        used = (await _used(db, [release.id])).get(release.id, 0)
        if used >= release.submission_limit:
            raise conflict(
                "vendor_limit_reached",
                "The submission limit for this release has been reached",
                limit=release.submission_limit,
            )
    email = normalize_email(data.email)
    if email is None:
        raise invalid("invalid_email", "Enter a valid email address")
    phone = normalize_phone(data.phone_e164) if data.phone_e164 else None
    sub_id = uuid.uuid4()
    file_id = None
    if resume:
        try:
            stored = await files_service.store_upload(
                ctx, db, resume, filename, RESUME_KINDS, "vendor_submission", sub_id
            )
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code in {"file_infected", "unsupported_file_type", "file_too_large", "scan_unavailable"}:
                raise invalid("file_rejected", "The resume was rejected", reason=code) from exc
            raise
        file_id = stored.id
    at = now()
    async with tenant_session(
        principal.tenant_id  # type: ignore[arg-type]
    ) as idb:  # system read (AD-26: never returned to the vendor)
        facts = await review.load_facts(
            idb,
            vendor_id=release.vendor_id,
            client_id=release.client_id,
            demand_id=release.demand_id,
            full_name=data.full_name,
            email=email,
            phone=phone,
            city=data.current_location,
            at=at,
        )
    findings = review.duplicate_findings(facts)
    sub = VendorSubmission(
        id=sub_id,
        tenant_id=release.tenant_id,
        human_id=await next_human_id(db, "VS", width=6),
        vendor_release_id=release.id,
        vendor_id=release.vendor_id,
        demand_id=release.demand_id,
        client_id=release.client_id,
        submitted_by_vendor_user_id=principal.id,
        full_name=data.full_name,
        email=email,
        phone_e164=phone,
        current_location=data.current_location,
        total_experience_months=data.total_experience_months,
        availability_date=data.availability_date,
        notice_period_days=data.notice_period_days,
        vendor_rate=data.vendor_rate,
        rate_unit=unit,
        currency=currency,
        resume_file_id=file_id,
        attestation_text=ATTESTATION_TEXT,
        attested_at=at,
        status="submitted",
        duplicate_findings=subs_svc.findings_payload(findings),
    )
    db.add(sub)
    await db.flush()
    subs_svc.add_timeline(db, sub, "Received")
    if findings:
        await subs_svc.set_status(
            ctx, db, sub, "duplicate", ActorRef("system", None), category=findings[0].category
        )
    await db.flush()
    emit(
        db,
        VendorSubmissionReceived(
            tenant_id=sub.tenant_id,
            submission_id=sub.id,
            release_id=release.id,
            vendor_id=sub.vendor_id,
            demand_id=sub.demand_id,
        ),
    )
    emit(db, VendorWork(sub.tenant_id, "after_submission", sub.id))
    return await _submission_out(db, sub, title=release.title)


async def _timeline(db: AsyncSession, sub_id: uuid.UUID) -> list[PortalTimelineItem]:
    rows = await db.execute(
        select(VendorSubmissionEvent.label, VendorSubmissionEvent.at)
        .where(VendorSubmissionEvent.submission_id == sub_id)
        .order_by(VendorSubmissionEvent.at, VendorSubmissionEvent.id)
    )
    return [PortalTimelineItem(label=label, at=at) for label, at in rows.tuples()]


def _locked(sub: VendorSubmission) -> bool:
    return sub.progress_key in LOCKED_PROGRESS or sub.status in {"rejected", "duplicate", "withdrawn"}


async def _submission_out(
    db: AsyncSession, sub: VendorSubmission, *, title: str | None = None, with_timeline: bool = True
) -> PortalSubmissionOut:
    return PortalSubmissionOut(
        id=sub.id,
        human_id=sub.human_id,
        release_id=sub.vendor_release_id,
        release_title=title,
        full_name=sub.full_name,
        email=sub.email,
        phone_e164=sub.phone_e164,
        current_location=sub.current_location,
        total_experience_months=sub.total_experience_months,
        availability_date=sub.availability_date,
        notice_period_days=sub.notice_period_days,
        vendor_rate=sub.vendor_rate,
        rate_unit=sub.rate_unit,
        currency=sub.currency,
        status=sub.status,
        label=subs_svc.vendor_label(sub),
        outcome_category=subs_svc.outcome_category(sub),
        next_interview_at=sub.next_interview_at,
        placed=sub.placement_id is not None and sub.progress_key == "placement",
        resume_file_id=sub.resume_file_id,
        resume_locked=_locked(sub),
        created_at=sub.created_at,
        timeline=await _timeline(db, sub.id) if with_timeline else [],
    )


async def list_submissions(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    principal: Principal,
    *,
    release_id: uuid.UUID | None = None,
    label: str | None = None,
) -> list[PortalSubmissionOut]:
    require_any_perm(principal, SUBMISSIONS_CREATE, RELEASES_VIEW)
    stmt = select(VendorSubmission, VendorRelease.title).join(
        VendorRelease, VendorRelease.id == VendorSubmission.vendor_release_id
    )
    if release_id is not None:
        stmt = stmt.where(VendorSubmission.vendor_release_id == release_id)
    rows = (
        await db.execute(stmt.order_by(VendorSubmission.created_at.desc(), VendorSubmission.id).limit(500))
    ).all()
    out = [await _submission_out(db, s, title=t, with_timeline=False) for s, t in rows]
    if label:
        out = [o for o in out if o.label == label]
    return out


async def _submission(db: AsyncSession, submission_id: uuid.UUID, *, lock: bool = False) -> VendorSubmission:
    stmt = select(VendorSubmission).where(VendorSubmission.id == submission_id)
    if lock:
        stmt = stmt.with_for_update()
    sub = (await db.execute(stmt)).scalar_one_or_none()
    if sub is None:
        raise NotFound("Submission not found")
    return sub


async def get_submission(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    principal: Principal,
    submission_id: uuid.UUID,
) -> PortalSubmissionOut:
    sub = await _submission(db, submission_id)
    require_any_perm(principal, SUBMISSIONS_CREATE, RELEASES_VIEW)
    title = (
        await db.execute(select(VendorRelease.title).where(VendorRelease.id == sub.vendor_release_id))
    ).scalar_one_or_none()
    return await _submission_out(db, sub, title=title)


async def withdraw(
    ctx: RequestContext, db: AsyncSession, principal: Principal, submission_id: uuid.UUID
) -> PortalSubmissionOut:
    from app.services import sla_service

    sub = await _submission(db, submission_id, lock=True)
    require_perm(principal, SUBMISSIONS_CREATE)
    await assert_can_operate(db, principal, "withdraw")
    await subs_svc.set_status(ctx, db, sub, "withdrawn", _actor(principal))
    await sla_service.cancel_clock(ctx, db, subs_svc.REVIEW_SUBJECT, sub.id, subs_svc.REVIEW_MILESTONE)
    return await _submission_out(db, sub)


async def revise_resume(
    ctx: RequestContext,
    db: AsyncSession,
    principal: Principal,
    submission_id: uuid.UUID,
    resume: bytes,
    filename: str | None,
) -> PortalSubmissionOut:
    sub = await _submission(db, submission_id, lock=True)
    require_perm(principal, SUBMISSIONS_CREATE)
    await assert_can_operate(db, principal, "upload")
    if _locked(sub):
        raise conflict("resume_locked", "The resume can no longer be replaced")
    try:
        stored = await files_service.store_upload(
            ctx, db, resume, filename, RESUME_KINDS, "vendor_submission", sub.id
        )
    except Exception as exc:
        code = getattr(exc, "code", None)
        if code in {"file_infected", "unsupported_file_type", "file_too_large", "scan_unavailable"}:
            raise invalid("file_rejected", "The resume was rejected", reason=code) from exc
        raise
    sub.resume_file_id = stored.id
    await db.flush()
    subs_svc.add_timeline(db, sub, "Resume updated")
    emit(db, VendorWork(sub.tenant_id, "resume_revised", sub.id))
    return await _submission_out(db, sub)


# ================================================================ placements, documents, statements, files
async def list_placements(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    principal: Principal,
) -> list[PortalPlacementOut]:
    require_perm(principal, PLACEMENTS_VIEW)
    rows = list(
        (
            await db.execute(select(Placement).order_by(Placement.planned_start_date.desc(), Placement.id))
        ).scalars()
    )
    titles: dict[uuid.UUID, str] = {}
    names: dict[uuid.UUID, str | None] = {}
    app_ids = [p.application_id for p in rows if p.application_id]
    if app_ids:
        for app_id, full_name, title in (
            await db.execute(
                select(VendorSubmission.application_id, VendorSubmission.full_name, VendorRelease.title)
                .join(VendorRelease, VendorRelease.id == VendorSubmission.vendor_release_id)
                .where(VendorSubmission.application_id.in_(app_ids))
            )
        ).tuples():
            if app_id is not None:
                names[app_id], titles[app_id] = full_name, title
    return [
        PortalPlacementOut(
            id=p.id,
            human_id=p.human_id,
            worker_name=names.get(p.application_id) if p.application_id else None,
            role_title=titles.get(p.application_id) if p.application_id else None,
            start_date=p.actual_start_date or p.planned_start_date,
            end_date=p.end_date or p.planned_end_date,
            status=p.status,
        )
        for p in rows
    ]


async def list_documents(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    principal: Principal,
) -> list[PortalDocumentOut]:
    require_perm(principal, DOCUMENTS_MANAGE)
    today = now().date()
    docs = (
        (
            await db.execute(
                select(Document)
                .where(
                    Document.owner_kind == "vendor",
                    Document.owner_id == principal.org_id,
                    Document.archived_at.is_(None),
                )
                .order_by(Document.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    pending = {
        u.document_id: u
        for u in (
            await db.execute(
                select(VendorDocumentUpload).where(VendorDocumentUpload.status == "pending_review")
            )
        ).scalars()
    }
    out = []
    for d in docs:
        from app.models.documents import DocumentVersion

        file_id = (
            (
                await db.execute(
                    select(DocumentVersion.file_id).where(DocumentVersion.id == d.current_version_id)
                )
            ).scalar_one_or_none()
            if d.current_version_id
            else None
        )
        out.append(
            PortalDocumentOut(
                id=d.id,
                category=d.category,
                title=d.title,
                expires_on=d.expires_on,
                status=doc_status(d.expires_on, today),
                pending_review=d.id in pending,
                file_id=file_id,
                created_at=d.created_at,
            )
        )
    return out


async def upload_document(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    principal: Principal,
    *,
    data: bytes,
    filename: str | None,
    category: str,
    title: str | None,
    expires_on: date | None,
    document_id: uuid.UUID | None = None,
) -> PortalDocumentOut:
    """Upload a renewal / new document from the portal.

    Stored as a ``documents`` row (or new version) through an internal system session, granted to the
    vendor org and flagged ``pending_review`` for the Vendor Manager.
    """
    from app.core import org_grants
    from app.services.documents import repository

    require_perm(principal, DOCUMENTS_MANAGE)
    vendor = await assert_can_operate(db, principal, "upload")
    if category not in {"agreement", "insurance", "registration", "other"}:
        raise invalid("invalid_category", "Unknown document category")
    sys_ctx = RequestContext(tenant_id=vendor.tenant_id, actor_kind="system")
    async with tenant_session(vendor.tenant_id) as idb:
        if document_id is not None:
            existing = (
                await idb.execute(
                    select(Document).where(
                        Document.id == document_id,
                        Document.owner_kind == "vendor",
                        Document.owner_id == vendor.id,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                raise NotFound("Document not found")
            file = await repository.store_bytes(sys_ctx, idb, existing.id, data, filename)
            await repository.append_version(sys_ctx, idb, existing, file, source="upload")
            if expires_on is not None:
                existing.expires_on = expires_on
            doc = existing
        else:
            doc_id = uuid.uuid4()
            file = await repository.store_bytes(sys_ctx, idb, doc_id, data, filename)
            doc = await repository.create_document(
                sys_ctx,
                idb,
                owner_kind="vendor",
                owner_id=vendor.id,
                category=category,
                title=title or file.original_name,
                expires_on=expires_on,
                document_id=doc_id,
            )
            await repository.append_version(sys_ctx, idb, doc, file, source="upload")
        await org_grants.grant(idb, "vendor", vendor.id, "document", doc.id, _actor(principal))
        idb.add(
            VendorDocumentUpload(
                id=uuid.uuid4(),
                tenant_id=vendor.tenant_id,
                vendor_id=vendor.id,
                document_id=doc.id,
                file_id=file.id,
                category=doc.category,
                expires_on=doc.expires_on,
                status="pending_review",
                uploaded_by_vendor_user_id=principal.id,
            )
        )
        await idb.flush()
        out = PortalDocumentOut(
            id=doc.id,
            category=doc.category,
            title=doc.title,
            expires_on=doc.expires_on,
            status=doc_status(doc.expires_on, now().date()),
            pending_review=True,
            file_id=file.id,
            created_at=doc.created_at,
        )
    return out


async def list_statements(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    principal: Principal,
) -> list[PortalStatementOut]:
    require_perm(principal, STATEMENTS_VIEW)
    rows = (
        await db.execute(
            select(VendorPayableStatement)
            .where(VendorPayableStatement.status == "finalized")
            .order_by(VendorPayableStatement.period_end.desc(), VendorPayableStatement.id)
        )
    ).scalars()
    return [
        PortalStatementOut(
            id=s.id,
            human_id=s.human_id,
            period_start=s.period_start,
            period_end=s.period_end,
            status=s.status,
            currency=s.currency,
            total_amount=s.total_amount,
            finalized_at=s.finalized_at,
        )
        for s in rows
    ]


async def statement_download(
    ctx: RequestContext, db: AsyncSession, principal: Principal, statement_id: uuid.UUID, fmt: str = "pdf"
) -> files_service.Download:
    require_perm(principal, STATEMENTS_VIEW)
    s = (
        await db.execute(
            select(VendorPayableStatement).where(
                VendorPayableStatement.id == statement_id, VendorPayableStatement.status == "finalized"
            )
        )
    ).scalar_one_or_none()
    if s is None:
        raise NotFound("Statement not found")
    file_id = s.csv_file_id if fmt == "csv" else s.file_id
    if file_id is None:
        raise NotFound("File not found")
    return await _read_file(ctx, db, file_id)


async def _read_file(ctx: RequestContext, db: AsyncSession, file_id: uuid.UUID) -> files_service.Download:  # ruff: ignore[unused-function-argument]
    f = (await db.execute(select(StoredFile).where(StoredFile.id == file_id))).scalar_one_or_none()
    if f is None or f.deleted_at is not None or f.scan_status not in files_service.DOWNLOADABLE_SCAN_STATUSES:
        raise NotFound("File not found")
    data = await files_service.read_file(f)
    return files_service.Download(data=data, filename=f.original_name, content_type=f.content_type)


async def download_file(
    ctx: RequestContext, db: AsyncSession, principal: Principal, file_id: uuid.UUID
) -> files_service.Download:
    """AD-13 for the portal: the file must belong to a row visible under vendor org RLS."""
    from app.models.documents import DocumentVersion

    visible = (
        await db.execute(
            select(VendorSubmission.id).where(VendorSubmission.resume_file_id == file_id).limit(1)
        )
    ).first() is not None
    if not visible:
        visible = (
            await db.execute(
                select(DocumentVersion.id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(
                    DocumentVersion.file_id == file_id,
                    Document.owner_kind == "vendor",
                    Document.owner_id == principal.org_id,
                )
                .limit(1)
            )
        ).first() is not None
    if not visible:
        visible = (
            await db.execute(
                select(VendorPayableStatement.id)
                .where(
                    VendorPayableStatement.status == "finalized",
                    (VendorPayableStatement.file_id == file_id)
                    | (VendorPayableStatement.csv_file_id == file_id),
                )
                .limit(1)
            )
        ).first() is not None
        if visible:
            require_perm(principal, STATEMENTS_VIEW)
    if not visible:
        raise NotFound("File not found")
    return await _read_file(ctx, db, file_id)


# ================================================================ dashboard, scorecard
async def dashboard(ctx: RequestContext, db: AsyncSession, principal: Principal) -> PortalDashboardOut:  # ruff: ignore[unused-function-argument]
    v = await _vendor(db, principal)
    open_releases = int(
        (
            await db.execute(
                select(func.count()).select_from(VendorRelease).where(VendorRelease.status == "active")
            )
        ).scalar_one()
    )
    subs = list((await db.execute(select(VendorSubmission))).scalars())
    counts = Counter(subs_svc.vendor_label(s) for s in subs)
    upcoming = sorted(
        (s for s in subs if s.next_interview_at is not None and s.next_interview_at > now()),
        key=lambda s: s.next_interview_at,  # type: ignore[arg-type,return-value]
    )[:10]
    active_placements = 0
    if PLACEMENTS_VIEW in principal.portal_permissions:
        active_placements = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(Placement)
                    .where(Placement.status.in_(("pre_joining", "joining_confirmed", "active")))
                )
            ).scalar_one()
        )
    return PortalDashboardOut(
        vendor_name=v.legal_name,
        vendor_status=v.status,
        open_releases=open_releases,
        submissions_by_label=[PortalLabelCount(label=k, count=n) for k, n in sorted(counts.items())],
        upcoming_interviews=[
            PortalUpcomingInterview(submission_id=s.id, full_name=s.full_name, at=s.next_interview_at)
            for s in upcoming
        ],
        active_placements=active_placements,
        can_operate=v.status == "active",
    )


async def scorecard(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,  # ruff: ignore[unused-function-argument]
    principal: Principal,
    period_from: date | None = None,
    period_to: date | None = None,
) -> PortalScorecardOut:
    from app.services.vendors import performance
    from app.services.vendors.common import load_settings

    if principal.org_id is None or principal.tenant_id is None:
        raise NotFound("Scorecard not found")
    # System read in an internal session (settings, interviews and offers are not vendor-visible tables).
    async with tenant_session(principal.tenant_id) as idb:
        if not (await load_settings(idb)).scorecard_visible_to_vendor:
            raise NotFound("Scorecard not found")
        card = await performance.vendor_scorecard(idb, principal.org_id, period_from, period_to)
    return PortalScorecardOut(
        period_from=card.period_from,
        period_to=card.period_to,
        releases_received=card.releases_received,
        submissions=card.submissions,
        acceptance_pct=card.acceptance_pct,
        duplicate_pct=card.duplicate_pct,
        submission_to_interview_pct=card.submission_to_interview_pct,
        interview_to_offer_pct=card.interview_to_offer_pct,
        placements=card.placements,
        median_hours_to_first_submission=card.median_hours_to_first_submission,
    )


def category_label(category: str | None) -> str | None:
    return CATEGORY_LABELS.get(category or "") if category else None
