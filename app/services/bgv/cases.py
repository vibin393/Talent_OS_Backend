# ruff: file-ignore[line-too-long]
"""BGV cases (Epic 20 A3, P2-7.1, FR-110): creation (event + ad-hoc), expansion, start, cancel, reads, document review.

* ``handle_requirements_evaluated`` (``ComplianceRequirementsEvaluated``): idempotent per ``(candidate, package)``.
  For a placement subject an open case of the same application, or a Clear case adjudicated inside
  ``reuse_clear_case_days``, is *linked* (``placement_id`` set) instead of recreated. New cases are ``required``.
* Ad-hoc cases (``POST /candidates/{id}/bgv-cases``) are ``required=False``: they raise no blocking gate.
* ``handle_placement_dropped`` cancels the placement's non-terminal cases (system, ``placement_dropped``),
  revokes provider / reference links and stops connector jobs.
"""

import uuid
from collections import Counter
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, secure_links
from app.core.actors import ActorRef
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.core.sequences import next_human_id
from app.events.bgv import BgvCaseOpened
from app.models.bgv import (
    CASE_TERMINAL,
    BgvCase,
    BgvCaseEvent,
    BgvCheck,
    BgvDiscrepancy,
    BgvDocumentRequest,
    BgvEvidence,
    BgvPackage,
    BgvProvider,
)
from app.schemas.bgv import (
    CaseEventOut,
    CaseListItem,
    CaseOut,
    CaseSummaryOut,
    CheckOut,
    DiscrepancyOut,
    DocumentRequestOut,
    DocumentReviewIn,
)
from app.services.bgv import status as st
from app.services.bgv.common import (
    CHECK_LABELS,
    actor,
    candidate_names,
    consent_active,
    doc_label,
    get_case,
    now,
    record_event,
    settings_row,
    tenant_of,
    user_id,
)

PROVIDER_LINK_PURPOSE = "bgv_provider_check"


# ================================================================ creation
def _snapshot(package: BgvPackage) -> dict[str, Any]:
    return {
        "id": str(package.id),
        "name": package.name,
        "checks": list(package.checks),
        "turnaround_target_days": package.turnaround_target_days,
    }


async def _expand(db: AsyncSession, case: BgvCase) -> None:
    checks = list(case.package_snapshot.get("checks") or [])
    keys: dict[str, bool] = {}
    for spec in checks:
        ctype = spec["type"]
        count = int(spec.get("count") or 1)
        docs = list(spec.get("required_documents") or [])
        for d in docs:
            keys.setdefault(d, True)
        for seq in range(1, count + 1):
            label = CHECK_LABELS.get(ctype, ctype)
            db.add(
                BgvCheck(
                    id=uuid.uuid4(),
                    tenant_id=case.tenant_id,
                    case_id=case.id,
                    candidate_id=case.candidate_id,
                    type=ctype,
                    seq=seq,
                    subject_label=f"{label} {seq}" if count > 1 else label,
                    document_request_keys=docs,
                    turnaround_days=int(spec.get("turnaround_days") or 5),
                    status="pending",
                )
            )
    for key in keys:
        label, kind = doc_label(key)
        db.add(
            BgvDocumentRequest(
                id=uuid.uuid4(),
                tenant_id=case.tenant_id,
                case_id=case.id,
                candidate_id=case.candidate_id,
                key=key,
                label=label,
                kind=kind,
                required=True,
                status="requested",
            )
        )
    await db.flush()


async def create_case(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    candidate_id: uuid.UUID,
    package: BgvPackage,
    placement_id: uuid.UUID | None,
    application_id: uuid.UUID | None,
    required: bool,
) -> BgvCase:
    tenant_id = tenant_of(ctx, db)
    case = BgvCase(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        human_id=await next_human_id(db, "bgv_case", "BGV", width=6),
        candidate_id=candidate_id,
        placement_id=placement_id,
        application_id=application_id,
        package_id=package.id,
        package_snapshot=_snapshot(package),
        required=required,
        status="initiated",
        created_by=user_id(ctx),
    )
    db.add(case)
    await db.flush()
    await _expand(db, case)
    record_event(db, case, "created", actor(ctx), to_status="initiated", required=required)
    emit(
        db,
        BgvCaseOpened(
            tenant_id=tenant_id,
            case_id=case.id,
            candidate_id=candidate_id,
            placement_id=placement_id,
            package_id=package.id,
            required=required,
        ),
    )
    return case


async def create_adhoc(
    ctx: RequestContext,
    db: AsyncSession,
    candidate_id: uuid.UUID,
    package_id: uuid.UUID,
    placement_id: uuid.UUID | None,
) -> CaseOut:
    from app.models.candidates import Candidate
    from app.models.placements import Placement

    if await db.get(Candidate, candidate_id) is None:
        raise NotFound("Candidate not found")
    package = await db.get(BgvPackage, package_id)
    if package is None or package.archived_at is not None:
        raise ValidationFailed(
            "Choose an active BGV package", details={"fields": {"package_id": "Unknown package"}}
        )
    if placement_id is not None:
        p = await db.get(Placement, placement_id)
        if p is None or p.candidate_id != candidate_id:
            raise ValidationFailed(
                "The placement does not belong to this candidate",
                details={"fields": {"placement_id": "Mismatch"}},
            )
        dup = (
            await db.execute(
                select(BgvCase.id).where(
                    BgvCase.placement_id == placement_id,
                    BgvCase.package_id == package_id,
                    BgvCase.status != "cancelled",
                )
            )
        ).first()
        if dup:
            raise ConflictError("This placement already has a case for the package", code="bgv_case_exists")
    case = await create_case(
        ctx,
        db,
        candidate_id=candidate_id,
        package=package,
        placement_id=placement_id,
        application_id=None,
        required=False,
    )
    return await detail(ctx, db, case.id)


async def handle_requirements_evaluated(
    ctx: RequestContext, db: AsyncSession, payload: dict[str, Any]
) -> int:
    """``ComplianceRequirementsEvaluated`` -> one required case per BGV package (idempotent, link or reuse)."""
    from app.models.placements import Placement

    candidate_id = uuid.UUID(str(payload["candidate_id"]))
    placement_id = uuid.UUID(str(payload["placement_id"])) if payload.get("placement_id") else None
    application_id: uuid.UUID | None = None
    if payload.get("subject_kind") == "application":
        application_id = uuid.UUID(str(payload["subject_id"]))
    elif placement_id is not None:
        p = await db.get(Placement, placement_id)
        application_id = p.application_id if p is not None else None
    settings = await settings_row(db, tenant_of(ctx, db))
    reuse_after = now() - timedelta(days=settings.reuse_clear_case_days)
    created = 0
    for raw in payload.get("bgv_package_ids") or ():
        package = await db.get(BgvPackage, uuid.UUID(str(raw)))
        if package is None:
            continue
        existing = list(
            (
                await db.execute(
                    select(BgvCase)
                    .where(
                        BgvCase.candidate_id == candidate_id,
                        BgvCase.package_id == package.id,
                        BgvCase.status.notin_(("cancelled", "failed")),
                    )
                    .order_by(BgvCase.created_at.desc())
                    .with_for_update()
                )
            ).scalars()
        )
        match: BgvCase | None = None
        for c in existing:
            if placement_id is not None and c.placement_id == placement_id:
                match = c
                break
            if c.placement_id is not None and c.placement_id != placement_id:
                continue
            same_app = application_id is not None and c.application_id == application_id
            fresh_clear = (
                c.status == "clear" and c.adjudicated_at is not None and c.adjudicated_at >= reuse_after
            )
            open_case = c.status not in CASE_TERMINAL
            if (
                (same_app and (open_case or c.status == "clear"))
                or fresh_clear
                or (placement_id is None and open_case)
            ):
                match = c
                break
        if match is not None:
            if placement_id is not None and match.placement_id is None:
                match.placement_id = placement_id
                match.required = True
                record_event(db, match, "linked", ActorRef("system", None), placement_id=placement_id)
                await db.flush()
                emit(
                    db,
                    BgvCaseOpened(
                        tenant_id=match.tenant_id,
                        case_id=match.id,
                        candidate_id=candidate_id,
                        placement_id=placement_id,
                        package_id=package.id,
                        required=True,
                    ),
                )
                if match.status == "clear":
                    from app.events.bgv import BgvCaseAdjudicated

                    emit(
                        db,
                        BgvCaseAdjudicated(
                            tenant_id=match.tenant_id,
                            case_id=match.id,
                            candidate_id=candidate_id,
                            placement_id=placement_id,
                            outcome="clear",
                            with_exceptions=match.outcome_qualifier == "with_exceptions",
                        ),
                    )
            continue
        await create_case(
            ctx,
            db,
            candidate_id=candidate_id,
            package=package,
            placement_id=placement_id,
            application_id=application_id,
            required=True,
        )
        created += 1
    return created


# ================================================================ start / cancel
async def start_case(ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID) -> CaseOut:
    from app.core.mailer import EmailMessage, send_email
    from app.core.settings import get_settings
    from app.services import entity_refs
    from app.services.bgv.events import BgvDocumentsRequested

    case = await get_case(db, case_id, lock=True)
    if case.status != "initiated":
        raise st.not_allowed(case.status, "documents_pending", "user")
    record_event(db, case, "started", actor(ctx))
    case.started_at = now()
    case.last_doc_reminder_at = now()
    await st.transition_case(db, case, "documents_pending", ActorRef("system", None))
    who = await entity_refs.addressee(db, "candidate", case.candidate_id)
    if who is not None and who.address:
        link = f"{get_settings().frontend_url.rstrip('/')}/portal/work/bgv"
        await send_email(
            EmailMessage(
                to=who.address,
                subject="Background verification: please review and upload your documents",
                text=(
                    f"Hello {(who.full_name or '').split(' ')[0]},\n\nAs part of your joining we need to verify some "
                    f"details. Please review the consent notice and upload the requested documents here:\n{link}\n"
                ),
                tenant_id=case.tenant_id,
                kind="bgv_documents_requested",
            )
        )
    emit(db, BgvDocumentsRequested(tenant_id=case.tenant_id, case_id=case.id, candidate_id=case.candidate_id))
    return await detail(ctx, db, case.id)


async def stop_work(db: AsyncSession, case_id: uuid.UUID, *, halt: str | None = None) -> None:
    """Revoke provider / reference links and stop queued connector jobs of a case (cancel / consent withdrawal)."""
    from app.models.bgv import Referee, ReferenceCheck
    from app.models.connectors import ConnectorJob

    checks = list((await db.execute(select(BgvCheck).where(BgvCheck.case_id == case_id))).scalars())
    for c in checks:
        await secure_links.revoke_for_subject(db, "bgv_check", c.id, PROVIDER_LINK_PURPOSE)
        if halt and c.status == "in_progress":
            c.halted_reason = halt
    job_ids = [c.connector_job_id for c in checks if c.connector_job_id]
    if job_ids:
        await db.execute(
            update(ConnectorJob)
            .where(ConnectorJob.id.in_(job_ids), ConnectorJob.status.in_(("queued", "failed")))
            .values(status="dead", last_error="cancelled", next_attempt_at=None)
        )
    ref_ids = [
        r
        for (r,) in (
            await db.execute(select(ReferenceCheck.id).where(ReferenceCheck.case_id == case_id))
        ).all()
    ]
    if ref_ids:
        for (rid,) in (
            await db.execute(select(Referee.id).where(Referee.reference_check_id.in_(ref_ids)))
        ).all():
            await secure_links.revoke_for_subject(db, "referee", rid)
    await db.flush()


async def cancel_case(
    ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID, reason: str, *, who: ActorRef | None = None
) -> CaseOut:
    case = await get_case(db, case_id, lock=True)
    who = who or actor(ctx)
    st.check_transition(case.status, "cancelled", who.kind)
    case.cancel_reason = reason
    await stop_work(db, case.id)
    await st.transition_case(db, case, "cancelled", who, reason=reason)
    audit.record("bgv.case_cancelled", "bgv_case", case.id, session=db, reason=reason)
    return await detail(ctx, db, case.id)


async def handle_placement_dropped(ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID) -> int:  # ruff: ignore[unused-function-argument]
    rows = list(
        (
            await db.execute(
                select(BgvCase).where(
                    BgvCase.placement_id == placement_id, BgvCase.status.notin_(CASE_TERMINAL)
                )
            )
        ).scalars()
    )
    for case in rows:
        case.cancel_reason = "placement_dropped"
        await stop_work(db, case.id)
        await st.transition_case(db, case, "cancelled", ActorRef("system", None), reason="placement_dropped")
    return len(rows)


# ================================================================ documents (review)
async def review_document(
    ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID, key: str, data: DocumentReviewIn
) -> DocumentRequestOut:
    case = await get_case(db, case_id, lock=True)
    if case.status in CASE_TERMINAL:
        raise ConflictError("The case is closed", code="case_closed")
    req = (
        await db.execute(
            select(BgvDocumentRequest).where(
                BgvDocumentRequest.case_id == case.id, BgvDocumentRequest.key == key
            )
        )
    ).scalar_one_or_none()
    if req is None:
        raise NotFound("Document request not found")
    if req.status != "submitted":
        raise ConflictError("Only submitted documents can be reviewed", code="document_not_submitted")
    if data.action == "accept":
        if key == "aadhaar" and req.file_ids and not data.redaction_confirmed:
            raise ValidationFailed(
                "Confirm the Aadhaar image is redacted (only the last 4 digits visible)",
                code="redaction_confirmation_required",
            )
        if key == "aadhaar" and req.file_ids:
            req.redaction_confirmed_by = user_id(ctx)
        req.status = "accepted"
        req.return_note = None
    else:
        if not data.note:
            raise ValidationFailed("Say what needs to change", details={"fields": {"note": "Required"}})
        req.status = "returned"
        req.return_note = data.note
        case.documents_submitted_at = None
    record_event(db, case, f"document_{data.action}ed", actor(ctx), key=key)
    await db.flush()
    await st.recompute_case_status(db, case)
    return DocumentRequestOut.model_validate(req)


# ================================================================ reads
async def _provider_names(db: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    return {  # ruff: ignore[unnecessary-comprehension]
        pid: name
        for pid, name in (
            await db.execute(select(BgvProvider.id, BgvProvider.name).where(BgvProvider.id.in_(ids)))
        ).all()
    }


async def check_out(db: AsyncSession, c: BgvCheck, providers: dict[uuid.UUID, str] | None = None) -> CheckOut:
    if providers is None:
        providers = await _provider_names(db, {c.provider_id} if c.provider_id else set())
    evidence = [
        f
        for (f,) in (await db.execute(select(BgvEvidence.file_id).where(BgvEvidence.check_id == c.id))).all()
    ]
    link_active = False
    if c.provider_link_id is not None:
        link = await db.get(secure_links.SecureLink, c.provider_link_id)
        link_active = bool(
            link and link.revoked_at is None and link.use_count < link.max_uses and link.expires_at > now()
        )
    out = CheckOut.model_validate(c)
    return out.model_copy(
        update={
            "provider_name": providers.get(c.provider_id) if c.provider_id else None,
            "evidence_file_ids": evidence,
            "provider_link_active": link_active,
        }
    )


async def _list_items(db: AsyncSession, cases: list[BgvCase]) -> list[CaseListItem]:
    from app.models.placements import Placement

    ids = [c.id for c in cases] or [uuid.uuid4()]
    names = await candidate_names(db, {c.candidate_id for c in cases})
    pkg_names = {c.id: (c.package_snapshot or {}).get("name") for c in cases}
    overdue = Counter(
        cid
        for (cid,) in (
            await db.execute(
                select(BgvCheck.case_id).where(BgvCheck.case_id.in_(ids), BgvCheck.overdue.is_(True))
            )
        ).all()
    )
    open_disc = Counter(
        cid
        for (cid,) in (
            await db.execute(
                select(BgvDiscrepancy.case_id).where(
                    BgvDiscrepancy.case_id.in_(ids), BgvDiscrepancy.status != "resolved"
                )
            )
        ).all()
    )
    pids = {c.placement_id for c in cases if c.placement_id}
    clients = (
        dict(
            (await db.execute(select(Placement.id, Placement.client_id).where(Placement.id.in_(pids)))).all()
        )
        if pids
        else {}
    )
    current = now()
    return [
        CaseListItem(
            id=c.id,
            human_id=c.human_id,
            candidate_id=c.candidate_id,
            candidate_name=names.get(c.candidate_id),
            placement_id=c.placement_id,
            client_id=clients.get(c.placement_id) if c.placement_id else None,
            package_id=c.package_id,
            package_name=pkg_names.get(c.id),
            required=c.required,
            status=c.status,
            outcome_qualifier=c.outcome_qualifier,
            overdue_checks=overdue.get(c.id, 0),
            open_discrepancies=open_disc.get(c.id, 0),
            created_at=c.created_at,
            age_days=max((current - c.created_at).days, 0),
        )
        for c in cases
    ]


async def list_cases(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    *,
    status: str | None = None,
    client_id: uuid.UUID | None = None,
    assignee_user_id: uuid.UUID | None = None,
    candidate_id: uuid.UUID | None = None,
    placement_id: uuid.UUID | None = None,
    overdue: bool | None = None,
    limit: int = 200,
) -> list[CaseListItem]:
    from app.models.placements import Placement

    stmt = select(BgvCase)
    if status:
        stmt = stmt.where(BgvCase.status.in_(status.split(",")))
    if candidate_id:
        stmt = stmt.where(BgvCase.candidate_id == candidate_id)
    if placement_id:
        stmt = stmt.where(BgvCase.placement_id == placement_id)
    if client_id:
        stmt = stmt.where(
            BgvCase.placement_id.in_(select(Placement.id).where(Placement.client_id == client_id))
        )
    if assignee_user_id:
        stmt = stmt.where(
            BgvCase.id.in_(select(BgvCheck.case_id).where(BgvCheck.assignee_user_id == assignee_user_id))
        )
    if overdue:
        stmt = stmt.where(BgvCase.id.in_(select(BgvCheck.case_id).where(BgvCheck.overdue.is_(True))))
    rows = list((await db.execute(stmt.order_by(BgvCase.created_at.desc()).limit(limit))).scalars())
    return await _list_items(db, rows)


async def detail(ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID) -> CaseOut:  # ruff: ignore[unused-function-argument]
    case = await get_case(db, case_id)
    (item,) = await _list_items(db, [case])
    docs = list(
        (
            await db.execute(
                select(BgvDocumentRequest)
                .where(BgvDocumentRequest.case_id == case.id)
                .order_by(BgvDocumentRequest.key)
            )
        ).scalars()
    )
    checks = list(
        (
            await db.execute(
                select(BgvCheck).where(BgvCheck.case_id == case.id).order_by(BgvCheck.type, BgvCheck.seq)
            )
        ).scalars()
    )
    providers = await _provider_names(db, {c.provider_id for c in checks if c.provider_id})
    discs = list(
        (
            await db.execute(
                select(BgvDiscrepancy)
                .where(BgvDiscrepancy.case_id == case.id)
                .order_by(BgvDiscrepancy.created_at)
            )
        ).scalars()
    )
    return CaseOut(
        **item.model_dump(),
        application_id=case.application_id,
        consent_active=await consent_active(db, case),
        consent_withdrawn_at=case.consent_withdrawn_at,
        adjudication_reasons=case.adjudication_reasons,
        adjudicated_by=case.adjudicated_by,
        adjudicated_at=case.adjudicated_at,
        cancel_reason=case.cancel_reason,
        evidence_purge_after=case.evidence_purge_after,
        evidence_purged_at=case.evidence_purged_at,
        started_at=case.started_at,
        documents=[DocumentRequestOut.model_validate(d) for d in docs],
        checks=[await check_out(db, c, providers) for c in checks],
        discrepancies=[DiscrepancyOut.model_validate(d) for d in discs],
        discrepancy_awaiting_response=any(d.status == "awaiting_response" for d in discs),
        lock_version=case.lock_version,
    )


async def timeline(ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID) -> list[CaseEventOut]:  # ruff: ignore[unused-function-argument]
    await get_case(db, case_id)
    rows = (
        await db.execute(
            select(BgvCaseEvent).where(BgvCaseEvent.case_id == case_id).order_by(BgvCaseEvent.at)
        )
    ).scalars()
    return [CaseEventOut.model_validate(r) for r in rows]


async def summaries(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    *,
    candidate_id: uuid.UUID | None = None,
    placement_id: uuid.UUID | None = None,
) -> list[CaseSummaryOut]:
    stmt = select(BgvCase)
    if candidate_id is not None:
        stmt = stmt.where(BgvCase.candidate_id == candidate_id)
    if placement_id is not None:
        stmt = stmt.where(BgvCase.placement_id == placement_id)
    rows = (await db.execute(stmt.order_by(BgvCase.created_at.desc()))).scalars()
    return [
        CaseSummaryOut(
            id=c.id,
            human_id=c.human_id,
            package_name=(c.package_snapshot or {}).get("name"),
            required=c.required,
            status=c.status,
            outcome_qualifier=c.outcome_qualifier,
            adjudicated_at=c.adjudicated_at,
        )
        for c in rows
    ]


async def count_open(db: AsyncSession) -> int:
    return int(
        (
            await db.execute(
                select(func.count()).select_from(BgvCase).where(BgvCase.status.notin_(CASE_TERMINAL))
            )
        ).scalar()
        or 0
    )


async def doc_reminders(db: AsyncSession) -> int:
    """Daily beat: remind candidates whose document set is incomplete every ``doc_reminder_every_days``."""
    from app.core.mailer import EmailMessage, send_email
    from app.services import entity_refs

    tenant_id = uuid.UUID(str(db.sync_session.info["tenant_id"]))
    settings = await settings_row(db, tenant_id)
    cutoff = now() - timedelta(days=settings.doc_reminder_every_days)
    rows = list(
        (
            await db.execute(
                select(BgvCase).where(
                    BgvCase.status == "documents_pending",
                    BgvCase.documents_submitted_at.is_(None),
                    (BgvCase.last_doc_reminder_at.is_(None)) | (BgvCase.last_doc_reminder_at <= cutoff),
                )
            )
        ).scalars()
    )
    sent = 0
    for case in rows:
        case.last_doc_reminder_at = now()
        who = await entity_refs.addressee(db, "candidate", case.candidate_id)
        if who is not None and who.address:
            await send_email(
                EmailMessage(
                    to=who.address,
                    subject="Reminder: documents for your background verification",
                    text="Some documents for your background verification are still pending. Please upload them in My Work.",
                    tenant_id=case.tenant_id,
                    kind="bgv_documents_reminder",
                )
            )
            sent += 1
    await db.flush()
    return sent
