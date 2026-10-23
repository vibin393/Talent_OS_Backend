# ruff: file-ignore[line-too-long]
"""Document checklist collection (Epic 46 A3, FR-234, AD-28).

* ``generate`` creates one ``case_document_requests`` row per checklist item of the pinned version (one per
  dependant for ``applies_to=dependant``), skipping existing keys.
* ``send``: ``collect_from=worker`` -> ``immigration_case_step`` (``documents_requested``) to the Candidate's portal
  (a Candidate without a portal account gets a Secure Link instead); ``collect_from=counsel`` -> one Secure Link
  (purpose ``immigration_upload``, 14 days, ``max_uses`` = number of items) emailed to the counsel address.
* Every upload path creates a P2 ``documents`` row: owner ``immigration_case``, ``worker_candidate_id`` = the
  Candidate, ``access_level='hr'``, ``retention_class='immigration'``. File rules (PDF / JPEG / PNG, size,
  malware scan) are P2's.
* ``verify`` / ``reject`` / ``waive`` need ``immigration:manage``; validity rules are evaluated on verify and
  returned as warnings.
"""

import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import column, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.immigration import CaseDependant, CaseDocumentRequest, ImmigrationCase, ImmigrationCounsel
from app.schemas.immigration import RequestOut, SendOut, VisaTypeDefinition
from app.services.immigration import cases as cases_svc
from app.services.immigration.common import (
    DOCUMENT_ACCESS,
    IMM_MANAGE,
    LINK_PURPOSE,
    OPEN_STATUSES,
    OWNER_KIND,
    RETENTION_CLASS,
    actor_json,
    conflict,
    invalid,
    not_found,
    now,
    require,
    tenant_of,
    today,
    user_id,
)

UPLOAD_KINDS = frozenset({"pdf", "png", "jpeg"})
MAX_UPLOAD_MB = 15
LINK_TTL = timedelta(days=14)
_placements = table("placements", column("id"), column("candidate_id"))
_candidate_emails = table("candidate_emails", column("candidate_id"), column("email"), column("is_primary"))


# ================================================================ output
async def _dependant_names(db: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = await db.execute(select(CaseDependant.id, CaseDependant.name).where(CaseDependant.id.in_(ids)))
    return dict(rows.tuples().all())


def to_out(
    r: CaseDocumentRequest, dep_names: dict[uuid.UUID, str], warnings: list[str] | None = None
) -> RequestOut:
    return RequestOut(
        id=r.id,
        case_id=r.case_id,
        checklist_key=r.checklist_key,
        label=r.label,
        document_category=r.document_category,
        dependant_id=r.dependant_id,
        dependant_name=dep_names.get(r.dependant_id) if r.dependant_id else None,
        collect_from=r.collect_from,
        mandatory=r.mandatory,
        status=r.status,
        document_id=r.document_id,
        rejection_reason=r.rejection_reason,
        requested_at=r.requested_at,
        uploaded_at=r.uploaded_at,
        verified_at=r.verified_at,
        due_date=r.due_date,
        warnings=warnings or [],
    )


async def outs(db: AsyncSession, rows: list[CaseDocumentRequest]) -> list[RequestOut]:
    names = await _dependant_names(db, {r.dependant_id for r in rows if r.dependant_id})
    return [to_out(r, names) for r in rows]


async def _rows(db: AsyncSession, case_id: uuid.UUID) -> list[CaseDocumentRequest]:
    return list(
        (
            await db.execute(
                select(CaseDocumentRequest)
                .where(CaseDocumentRequest.case_id == case_id)
                .order_by(CaseDocumentRequest.created_at, CaseDocumentRequest.label)
            )
        ).scalars()
    )


async def list_requests(ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID) -> list[RequestOut]:
    await cases_svc.load_scoped(ctx, db, case_id)
    return await outs(db, await _rows(db, case_id))


# ================================================================ generate / send
async def generate(ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID) -> list[RequestOut]:
    case = await cases_svc.load_scoped(ctx, db, case_id, write=True)
    if case.status not in OPEN_STATUSES:
        raise conflict("case_closed", "The checklist of a closed case cannot change")
    _, d = await cases_svc.pinned(db, case)
    existing = {(r.checklist_key, r.dependant_id) for r in await _rows(db, case.id)}
    deps = list(
        (
            await db.execute(
                select(CaseDependant)
                .where(CaseDependant.case_id == case.id)
                .order_by(CaseDependant.created_at)
            )
        ).scalars()
    )
    for item in d.checklist:
        targets: list[CaseDependant | None] = list(deps) if item.applies_to == "dependant" else [None]
        for dep in targets:
            dep_id = dep.id if dep is not None else None
            if (item.key, dep_id) in existing:
                continue
            label = f"{item.label} — {dep.name}" if dep is not None else item.label
            db.add(
                CaseDocumentRequest(
                    id=uuid.uuid4(),
                    tenant_id=case.tenant_id,
                    case_id=case.id,
                    candidate_id=case.candidate_id,
                    checklist_key=item.key,
                    label=label[:200],
                    document_category=item.document_category,
                    dependant_id=dep_id,
                    collect_from=item.collect_from,
                    mandatory=item.mandatory,
                    status="requested",
                )
            )
    await db.flush()
    return await outs(db, await _rows(db, case.id))


async def _is_worker(db: AsyncSession, candidate_id: uuid.UUID) -> bool:
    """Tell whether the Candidate is a Worker (has a Placement): Workers use the "My Work" portal."""
    return (
        await db.execute(select(_placements.c.id).where(_placements.c.candidate_id == candidate_id).limit(1))
    ).first() is not None


async def _candidate_email(db: AsyncSession, candidate_id: uuid.UUID) -> str | None:
    row = (
        await db.execute(
            select(_candidate_emails.c.email)
            .where(_candidate_emails.c.candidate_id == candidate_id)
            .order_by(_candidate_emails.c.is_primary.desc())
            .limit(1)
        )
    ).first()
    return str(row.email) if row else None


async def _issue_link(
    ctx: RequestContext, db: AsyncSession, case: ImmigrationCase, request_ids: list[uuid.UUID], audience: str
) -> tuple[uuid.UUID, str]:
    from app.core import secure_links

    link, token = await secure_links.issue(
        db,
        tenant_id=case.tenant_id,
        purpose=LINK_PURPOSE,
        subject_type="immigration_case",
        subject_id=case.id,
        ttl=LINK_TTL,
        max_uses=max(1, len(request_ids)),
        metadata={"request_ids": [str(i) for i in request_ids], "audience": audience},
        created_by=user_id(ctx),
    )
    return link.id, token


async def _email(case: ImmigrationCase, to: str, subject: str, body: str) -> None:
    from app.core.mailer import EmailMessage, send_email

    await send_email(
        EmailMessage(to=to, subject=subject, text=body, tenant_id=case.tenant_id, kind="immigration_upload")
    )


async def send(
    ctx: RequestContext,
    db: AsyncSession,
    case_id: uuid.UUID,
    request_ids: list[uuid.UUID],
    due_date: date | None,
) -> SendOut:
    from app.core.secure_links import link_url
    from app.services.immigration.events import ImmigrationCaseStep

    case = await cases_svc.load_scoped(ctx, db, case_id, write=True)
    if case.status not in OPEN_STATUSES:
        raise conflict("case_closed", "Documents cannot be requested on a closed case")
    rows = [r for r in await _rows(db, case.id) if r.id in set(request_ids)]
    if len(rows) != len(set(request_ids)):
        raise not_found("Document request")
    sendable = [r for r in rows if r.status in ("requested", "rejected")]
    out = SendOut(notified_worker=0, worker_links=0, counsel_links=0, internal=0)
    stamp = now()
    for r in sendable:
        r.requested_at = stamp
        if due_date is not None:
            r.due_date = due_date
    worker = [r for r in sendable if r.collect_from == "worker"]
    counsel = [r for r in sendable if r.collect_from == "counsel"]
    out.internal = sum(1 for r in sendable if r.collect_from == "internal")
    if worker:
        if await _is_worker(db, case.candidate_id):
            from app.core.events import emit

            emit(
                db,
                ImmigrationCaseStep(
                    tenant_id=case.tenant_id,
                    case_id=case.id,
                    candidate_id=case.candidate_id,
                    step="documents_requested",
                    actor_user_id=user_id(ctx),
                ),
            )
            out.notified_worker = len(worker)
        else:
            email = await _candidate_email(db, case.candidate_id)
            if not email:
                raise invalid(
                    "candidate_email_required", "The candidate has no email address for the upload link"
                )
            link_id, token = await _issue_link(ctx, db, case, [r.id for r in worker], "candidate")
            for r in worker:
                r.secure_link_id = link_id
            await _email(
                case,
                email,
                f"Documents requested for {case.human_id}",
                f"Please upload the requested documents for {case.human_id}: {link_url(token)} (valid for 14 days).",
            )
            out.worker_links = 1
    if counsel:
        c = await db.get(ImmigrationCounsel, case.counsel_id) if case.counsel_id else None
        if c is None or not c.email:
            raise invalid("counsel_email_required", "Add counsel with an email address to the case first")
        link_id, token = await _issue_link(ctx, db, case, [r.id for r in counsel], "counsel")
        for r in counsel:
            r.secure_link_id = link_id
        await _email(
            case,
            str(c.email),
            f"Documents requested for {case.human_id}",
            f"Please upload the requested documents for {case.human_id}: {link_url(token)} (valid for 14 days).",
        )
        out.counsel_links = 1
    from app.models.immigration import CaseEvent

    if sendable:
        db.add(
            CaseEvent(
                id=uuid.uuid4(),
                tenant_id=case.tenant_id,
                case_id=case.id,
                candidate_id=case.candidate_id,
                kind="document_requested",
                actor=actor_json(ctx),
                payload={"items": [r.checklist_key for r in sendable], "count": len(sendable)},
            )
        )
    await db.flush()
    return out


# ================================================================ upload (shared by every path)
async def store_upload(
    db: AsyncSession,
    req: CaseDocumentRequest,
    data: bytes,
    filename: str | None,
    *,
    actor: dict[str, Any],
    via: str,
) -> CaseDocumentRequest:
    from app.core.events import emit
    from app.models.immigration import CaseEvent
    from app.services.documents import repository
    from app.services.immigration.events import (
        ImmigrationCaseStep,  # ruff: ignore[unused-import]  (catalogue import check)  # noqa: F401
    )

    if req.status in ("verified", "waived"):
        raise conflict("request_closed", "This item is already complete")
    case = await db.get(ImmigrationCase, req.case_id)
    if case is None or case.status not in (*OPEN_STATUSES, "approved"):
        raise conflict("case_closed", "This case no longer accepts documents")
    ctx = RequestContext(tenant_id=tenant_of(db), actor_kind="system")
    doc_id = uuid.uuid4()
    stored = await repository.store_bytes(ctx, db, doc_id, data, filename, kinds=UPLOAD_KINDS)
    doc = await repository.create_document(
        ctx,
        db,
        owner_kind=OWNER_KIND,
        owner_id=req.case_id,
        category=req.document_category,
        title=req.label,
        access_level=DOCUMENT_ACCESS,
        worker_candidate_id=req.candidate_id,
        retention_class=RETENTION_CLASS,
        document_id=doc_id,
    )
    await repository.append_version(ctx, db, doc, stored, source="upload")
    req.document_id = doc.id
    req.status = "uploaded"
    req.uploaded_at = now()
    req.rejection_reason = None
    db.add(
        CaseEvent(
            id=uuid.uuid4(),
            tenant_id=req.tenant_id,
            case_id=req.case_id,
            candidate_id=req.candidate_id,
            kind="document_uploaded",
            actor=actor,
            payload={"request_id": str(req.id), "item": req.checklist_key, "via": via},
        )
    )
    await db.flush()
    _ = emit  # uploads notify nobody (the owner sees the checklist)
    return req


async def upload_internal(
    ctx: RequestContext, db: AsyncSession, request_id: uuid.UUID, data: bytes, filename: str | None
) -> RequestOut:
    req = await _load_request(ctx, db, request_id, write=True)
    await store_upload(db, req, data, filename, actor=actor_json(ctx), via="internal")
    return (await outs(db, [req]))[0]


# ================================================================ review
async def _load_request(
    ctx: RequestContext, db: AsyncSession, request_id: uuid.UUID, *, write: bool
) -> CaseDocumentRequest:
    req = await db.get(CaseDocumentRequest, request_id)
    if req is None:
        raise not_found("Document request")
    await cases_svc.load_scoped(ctx, db, req.case_id, write=write)
    return req


def _months_between(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month) - (1 if b.day < a.day else 0)


async def validity_warnings(
    db: AsyncSession, case: ImmigrationCase, req: CaseDocumentRequest, d: VisaTypeDefinition
) -> list[str]:
    item = next((i for i in d.checklist if i.key == req.checklist_key), None)
    if item is None or item.validity_rule is None:
        return []
    out: list[str] = []
    rule = item.validity_rule
    travel = (case.key_dates or {}).get("travel_date")
    if rule.min_months_remaining:
        doc_expiry = None
        if req.document_id is not None:
            from app.models.documents import Document

            doc = await db.get(Document, req.document_id)
            doc_expiry = doc.expires_on if doc is not None else None
        anchor = date.fromisoformat(str(travel)) if travel else today()
        if doc_expiry is None:
            out.append(
                f"Check that it is valid at least {rule.min_months_remaining} months beyond {'travel' if travel else 'today'}"
            )
        elif _months_between(anchor, doc_expiry) < rule.min_months_remaining:
            out.append(
                f"Valid less than {rule.min_months_remaining} months beyond {'travel' if travel else 'today'}"
            )
    if rule.max_age_days:
        out.append(f"Check that it is not older than {rule.max_age_days} days")
    return out


async def verify(
    ctx: RequestContext, db: AsyncSession, request_id: uuid.UUID
) -> tuple[RequestOut, list[str]]:
    from app.models.immigration import CaseEvent

    require(ctx, IMM_MANAGE)
    req = await _load_request(ctx, db, request_id, write=True)
    if req.status != "uploaded":
        raise conflict("request_not_uploaded", "Only an uploaded item can be verified")
    case = await db.get(ImmigrationCase, req.case_id)
    assert case is not None
    _, d = await cases_svc.pinned(db, case)
    warnings = await validity_warnings(db, case, req, d)
    req.status = "verified"
    req.verified_by = user_id(ctx)
    req.verified_at = now()
    db.add(
        CaseEvent(
            id=uuid.uuid4(),
            tenant_id=req.tenant_id,
            case_id=req.case_id,
            candidate_id=req.candidate_id,
            kind="document_verified",
            actor=actor_json(ctx),
            payload={"request_id": str(req.id), "item": req.checklist_key},
        )
    )
    await db.flush()
    names = await _dependant_names(db, {req.dependant_id} if req.dependant_id else set())
    return to_out(req, names, warnings), warnings


async def reject(ctx: RequestContext, db: AsyncSession, request_id: uuid.UUID, reason: str) -> RequestOut:
    from app.models.immigration import CaseEvent

    req = await _load_request(ctx, db, request_id, write=True)
    if req.status != "uploaded":
        raise conflict("request_not_uploaded", "Only an uploaded item can be rejected")
    req.status = "rejected"
    req.rejection_reason = reason.strip()
    req.verified_by = None
    req.verified_at = None
    db.add(
        CaseEvent(
            id=uuid.uuid4(),
            tenant_id=req.tenant_id,
            case_id=req.case_id,
            candidate_id=req.candidate_id,
            kind="document_rejected",
            actor=actor_json(ctx),
            payload={"request_id": str(req.id), "item": req.checklist_key},
        )
    )
    await db.flush()
    if req.collect_from in ("worker", "counsel"):  # re-request notifies again
        await send(ctx, db, req.case_id, [req.id], req.due_date)
    return (await outs(db, [req]))[0]


async def waive(ctx: RequestContext, db: AsyncSession, request_id: uuid.UUID, reason: str) -> RequestOut:
    from app.models.immigration import CaseEvent

    req = await _load_request(ctx, db, request_id, write=True)
    if req.status in ("verified", "waived"):
        raise conflict("request_closed", "This item is already complete")
    req.status = "waived"
    req.rejection_reason = reason.strip()  # the waiver reason (internal; never shown on the portal)
    db.add(
        CaseEvent(
            id=uuid.uuid4(),
            tenant_id=req.tenant_id,
            case_id=req.case_id,
            candidate_id=req.candidate_id,
            kind="note",
            actor=actor_json(ctx),
            payload={"request_id": str(req.id), "item": req.checklist_key, "waived": True},
        )
    )
    await db.flush()
    return (await outs(db, [req]))[0]
