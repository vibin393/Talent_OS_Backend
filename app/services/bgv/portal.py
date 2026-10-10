# ruff: noqa: E501
"""Worker "My Work" BGV (Epic 20 A4/A6/A7; ``/api/v1/portal/work``). Runs in the worker session
(``OrgScope.of("worker", candidate_id)``): another candidate's case is invisible (org RLS) -> 404. Needs no
Placement (BGV can start at *Selected*).

Never returned: internal notes, verifier details, provider names (other than in the consent view), adjudication
reasons, reference answers. Aadhaar: only ``aadhaar_last4`` + a redacted image; a full number anywhere -> 422
``aadhaar_full_number_forbidden``.
"""

import hashlib
import json
import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.models.bgv import (
    CASE_TERMINAL,
    BgvCase,
    BgvCheck,
    BgvDiscrepancy,
    BgvDocumentRequest,
    BgvProvider,
    Referee,
    ReferenceCheck,
)
from app.schemas.bgv_portal import (
    AddressDeclaration,
    EducationDeclaration,
    EmploymentDeclaration,
    PortalCaseOut,
    PortalConsentIn,
    PortalConsentView,
    PortalDiscrepancyOut,
    PortalDocumentOut,
    PortalRefereeOut,
    PortalReferenceCheckOut,
    RefereesIn,
    RespondIn,
)
from app.services.bgv import status as st
from app.services.bgv.common import (
    BGV_PURPOSE,
    REFERENCE_PURPOSE,
    UPLOAD_KINDS,
    consent_active,
    ensure_notice,
    now,
    record_event,
)

DECLARATION_MODELS: dict[str, Any] = {
    "employment_history": EmploymentDeclaration,
    "education_history": EducationDeclaration,
    "address_history": AddressDeclaration,
}
_STAGE = {
    "initiated": "not_started",
    "documents_pending": "documents",
    "in_verification": "verification",
    "discrepancy": "verification",
    "under_review": "verification",
    "clear": "decided",
    "failed": "decided",
}


def _who(candidate_id: uuid.UUID) -> ActorRef:
    return ActorRef("candidate", candidate_id)


def _ip_hash(ctx: RequestContext) -> str | None:
    return hashlib.sha256(ctx.ip.encode()).hexdigest() if ctx.ip else None


async def _case(
    db: AsyncSession, candidate_id: uuid.UUID, case_id: uuid.UUID, *, lock: bool = False
) -> BgvCase:
    stmt = select(BgvCase).where(
        BgvCase.id == case_id, BgvCase.candidate_id == candidate_id, BgvCase.status != "cancelled"
    )
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("BGV case not found")
    return row


async def _open_case(db: AsyncSession, candidate_id: uuid.UUID, case_id: uuid.UUID) -> BgvCase:
    case = await _case(db, candidate_id, case_id, lock=True)
    if case.status in CASE_TERMINAL or case.status == "initiated":
        raise ConflictError("This verification is not collecting documents", code="case_not_collecting")
    return case


def _doc_out(r: BgvDocumentRequest) -> PortalDocumentOut:
    declared = dict(r.declared) if r.declared else None
    return PortalDocumentOut(
        key=r.key,
        label=r.label,
        kind=r.kind,
        required=r.required,
        status=r.status,
        file_count=len(r.file_ids or []),
        declared=declared,
        return_note=r.return_note,
    )


async def _case_out(db: AsyncSession, case: BgvCase) -> PortalCaseOut:
    docs = list(
        (
            await db.execute(
                select(BgvDocumentRequest)
                .where(BgvDocumentRequest.case_id == case.id)
                .order_by(BgvDocumentRequest.key)
            )
        ).scalars()
    )
    types = sorted({c["type"] for c in (case.package_snapshot or {}).get("checks") or []})
    return PortalCaseOut(
        id=case.id,
        human_id=case.human_id,
        status="clear" if case.status == "clear" else "failed" if case.status == "failed" else "in_progress",
        stage=_STAGE.get(case.status, "verification"),
        consent_given=await consent_active(db, case),
        check_types=types,
        documents=[_doc_out(d) for d in docs],
        documents_submitted=case.documents_submitted_at is not None,
        created_at=case.created_at,
    )


async def list_cases(db: AsyncSession, candidate_id: uuid.UUID) -> list[PortalCaseOut]:
    rows = (
        await db.execute(
            select(BgvCase)
            .where(BgvCase.candidate_id == candidate_id, BgvCase.status != "cancelled")
            .order_by(BgvCase.created_at.desc())
        )
    ).scalars()
    return [await _case_out(db, c) for c in rows]


async def get_case(db: AsyncSession, candidate_id: uuid.UUID, case_id: uuid.UUID) -> PortalCaseOut:
    return await _case_out(db, await _case(db, candidate_id, case_id))


# ================================================================ consent
async def consent_view(
    db: AsyncSession, tenant_id: uuid.UUID, candidate_id: uuid.UUID, case_id: uuid.UUID
) -> PortalConsentView:
    case = await _case(db, candidate_id, case_id)
    notice = await ensure_notice(db, tenant_id, BGV_PURPOSE)
    pids = {
        p
        for (p,) in (
            await db.execute(
                select(BgvCheck.provider_id).where(
                    BgvCheck.case_id == case.id, BgvCheck.provider_id.isnot(None)
                )
            )
        ).all()
    }
    providers = (
        [n for (n,) in (await db.execute(select(BgvProvider.name).where(BgvProvider.id.in_(pids)))).all()]
        if pids
        else []
    )
    return PortalConsentView(
        case_id=case.id,
        notice_id=notice.id,
        title=notice.title,
        body_md=notice.body_md,
        version=notice.version,
        check_types=sorted({c["type"] for c in (case.package_snapshot or {}).get("checks") or []}),
        providers=sorted(providers),
        consent_given=await consent_active(db, case),
    )


async def give_consent(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, case_id: uuid.UUID, data: PortalConsentIn
) -> PortalCaseOut:
    from app.services.privacy.consent import grant_consent

    if not data.agreed:
        raise ValidationFailed("Agree to the notice to continue", details={"fields": {"agreed": "Required"}})
    case = await _case(db, candidate_id, case_id, lock=True)
    if case.status in CASE_TERMINAL:
        raise ConflictError("This verification is closed", code="case_closed")
    view = await consent_view(db, case.tenant_id, candidate_id, case_id)
    if view.notice_id != data.notice_id:
        raise ConflictError("The notice changed; review it again", code="notice_changed")
    checks = list((await db.execute(select(BgvCheck).where(BgvCheck.case_id == case.id))).scalars())
    consent = await grant_consent(
        ctx,
        db,
        candidate_id,
        BGV_PURPOSE,
        source="candidate_portal",
        notice_id=data.notice_id,
        evidence={
            "case_id": str(case.id),
            "checks": sorted({c.type for c in checks}),
            "provider_ids": sorted({str(c.provider_id) for c in checks if c.provider_id}),
            "ip_hash": _ip_hash(ctx),
            "ua": (ctx.user_agent or "")[:300],
        },
    )
    case.consent_id = consent.id
    case.consent_withdrawn_at = None
    record_event(db, case, "consent_given", _who(candidate_id))
    await db.flush()
    await st.recompute_case_status(db, case)
    return await _case_out(db, case)


# ================================================================ documents
def _validate_declaration(key: str, raw: Any) -> dict[str, Any]:
    from app.core.identity_docs import assert_no_full_aadhaar

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise ValidationFailed("The declaration is not valid JSON", code="invalid_declaration") from exc
    if not isinstance(raw, dict):
        raise ValidationFailed("The declaration must be an object", code="invalid_declaration")
    assert_no_full_aadhaar(*_strings(raw))
    model = DECLARATION_MODELS.get(key)
    if model is None:
        return {k: v for k, v in raw.items() if isinstance(v, str | int | float | bool) and k != "aadhaar"}
    try:
        return dict(model.model_validate(raw).model_dump(mode="json"))
    except ValidationError as exc:
        fields = {".".join(str(p) for p in e["loc"]): e["msg"] for e in exc.errors()}
        raise ValidationFailed(
            "Please check the declaration", code="invalid_declaration", details={"fields": fields}
        ) from exc


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def _aadhaar_last4(value: str | None) -> str:
    from app.core.identity_docs import assert_no_full_aadhaar, validate_aadhaar_last4

    raw = (value or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 12:
        raise ValidationFailed(
            "Never enter your full Aadhaar number; enter only the last 4 digits",
            code="aadhaar_full_number_forbidden",
        )
    assert_no_full_aadhaar(raw)
    return validate_aadhaar_last4(raw)


async def save_document(
    ctx: RequestContext,
    db: AsyncSession,
    candidate_id: uuid.UUID,
    case_id: uuid.UUID,
    key: str,
    *,
    uploads: list[tuple[bytes, str | None]],
    declaration: Any = None,
    aadhaar_last4: str | None = None,
) -> PortalDocumentOut:
    from app.services import files_service

    case = await _open_case(db, candidate_id, case_id)
    req = (
        await db.execute(
            select(BgvDocumentRequest)
            .where(BgvDocumentRequest.case_id == case.id, BgvDocumentRequest.key == key)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if req is None:
        raise NotFound("Document request not found")
    if req.status == "accepted":
        raise ConflictError("This document was already accepted", code="document_accepted")
    declared: dict[str, Any] = dict(req.declared or {})
    if key == "aadhaar":
        if aadhaar_last4 is None and not declared.get("aadhaar_last4"):
            raise ValidationFailed(
                "Enter the last 4 digits of your Aadhaar", details={"fields": {"aadhaar_last4": "Required"}}
            )
        if aadhaar_last4 is not None:
            declared["aadhaar_last4"] = _aadhaar_last4(aadhaar_last4)
            declared["redaction_attested"] = True
    if declaration is not None:
        declared.update(_validate_declaration(key, declaration))
    if req.kind == "declaration" and declaration is None and not declared:
        raise ValidationFailed("Fill in the declaration", details={"fields": {"declaration": "Required"}})
    if req.kind == "upload" and not uploads and not req.file_ids:
        raise ValidationFailed("Upload the document", details={"fields": {"file": "Required"}})
    file_ids = list(req.file_ids or [])
    for content, name in uploads[:5]:
        stored = await files_service.store_upload(ctx, db, content, name, UPLOAD_KINDS, "bgv_case", case.id)
        file_ids.append(stored.id)
    req.file_ids = file_ids
    req.declared = declared or None
    req.status = "submitted"
    req.return_note = None
    req.submitted_at = now()
    await _relabel(db, case, key, declared)
    record_event(db, case, "document_submitted", _who(candidate_id), key=key)
    await db.flush()
    return _doc_out(req)


async def _relabel(db: AsyncSession, case: BgvCase, key: str, declared: dict[str, Any]) -> None:
    """Employment / education declarations name the subjects of the checks ("Employer 2: Infosys")."""
    field = {
        "employment_history": ("employment", "employer"),
        "education_history": ("education", "institution"),
    }.get(key)
    entries = declared.get("entries") if declared else None
    if field is None or not entries:
        return
    ctype, attr = field
    checks = (
        await db.execute(
            select(BgvCheck).where(BgvCheck.case_id == case.id, BgvCheck.type == ctype).order_by(BgvCheck.seq)
        )
    ).scalars()
    for check, entry in zip(checks, entries, strict=False):
        base = check.subject_label.split(":", 1)[0]
        check.subject_label = f"{base}: {entry.get(attr)}"[:300]


async def submit_documents(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    candidate_id: uuid.UUID,
    case_id: uuid.UUID,
) -> PortalCaseOut:
    case = await _open_case(db, candidate_id, case_id)
    missing = [
        r.key
        for r in (
            await db.execute(select(BgvDocumentRequest).where(BgvDocumentRequest.case_id == case.id))
        ).scalars()
        if r.required and r.status not in ("submitted", "accepted")
    ]
    if missing:
        raise ValidationFailed(
            "Some documents are still missing", code="documents_missing", details={"keys": missing}
        )
    case.documents_submitted_at = now()
    record_event(db, case, "documents_submitted", _who(candidate_id))
    await db.flush()
    await st.recompute_case_status(db, case)
    return await _case_out(db, case)


# ================================================================ discrepancies
async def list_discrepancies(db: AsyncSession, candidate_id: uuid.UUID) -> list[PortalDiscrepancyOut]:
    rows = (
        await db.execute(
            select(BgvDiscrepancy, BgvCheck.type, BgvCase.status)
            .join(BgvCheck, BgvCheck.id == BgvDiscrepancy.check_id)
            .join(BgvCase, BgvCase.id == BgvDiscrepancy.case_id)
            .where(BgvCase.candidate_id == candidate_id, BgvCase.status != "cancelled")
            .order_by(BgvDiscrepancy.created_at.desc())
        )
    ).all()
    return [
        PortalDiscrepancyOut(
            id=d.id,
            case_id=d.case_id,
            check_type=ctype,
            severity=d.severity,
            description=d.description,
            status=d.status,
            response_due_on=d.response_due_on,
            candidate_response=d.candidate_response,
            responded_at=d.responded_at,
        )
        for d, ctype, _ in rows
    ]


async def respond(
    ctx: RequestContext,
    db: AsyncSession,
    candidate_id: uuid.UUID,
    discrepancy_id: uuid.UUID,
    data: RespondIn,
    uploads: list[tuple[bytes, str | None]],
) -> PortalDiscrepancyOut:
    from app.core.identity_docs import assert_no_full_aadhaar
    from app.services import files_service

    assert_no_full_aadhaar(data.response)
    d = (
        await db.execute(select(BgvDiscrepancy).where(BgvDiscrepancy.id == discrepancy_id).with_for_update())
    ).scalar_one_or_none()
    if d is None:
        raise NotFound("Discrepancy not found")
    case = await _case(db, candidate_id, d.case_id, lock=True)
    if d.status != "awaiting_response" or case.status in CASE_TERMINAL:
        raise ConflictError("This discrepancy no longer accepts a response", code="response_closed")
    ids = list(d.response_file_ids or [])
    for content, name in uploads[:5]:
        stored = await files_service.store_upload(ctx, db, content, name, UPLOAD_KINDS, "bgv_case", case.id)
        ids.append(stored.id)
    d.response_file_ids = ids
    d.candidate_response = data.response
    d.responded_at = now()
    d.status = "responded"
    record_event(db, case, "discrepancy_responded", _who(candidate_id), discrepancy_id=d.id)
    await db.flush()
    await st.recompute_case_status(db, case)
    ctype = (await db.execute(select(BgvCheck.type).where(BgvCheck.id == d.check_id))).scalar_one()
    return PortalDiscrepancyOut(
        id=d.id,
        case_id=d.case_id,
        check_type=ctype,
        severity=d.severity,
        description=d.description,
        status=d.status,
        response_due_on=d.response_due_on,
        candidate_response=d.candidate_response,
        responded_at=d.responded_at,
    )


# ================================================================ reference checks
async def _ref_out(db: AsyncSession, rc: ReferenceCheck, notice: Any) -> PortalReferenceCheckOut:
    from app.services.privacy.consent import has_active_consent

    refs = (
        await db.execute(
            select(Referee).where(Referee.reference_check_id == rc.id).order_by(Referee.created_at)
        )
    ).scalars()
    given = rc.consent_id is not None and await has_active_consent(db, rc.candidate_id, REFERENCE_PURPOSE)
    return PortalReferenceCheckOut(
        id=rc.id,
        status=rc.status,
        min_referees=rc.min_referees,
        consent_given=given,
        notice_id=notice.id if notice else None,
        notice_title=notice.title if notice else None,
        notice_body_md=notice.body_md if notice else None,
        referees=[
            PortalRefereeOut(
                id=r.id, name=r.name, relationship=r.relationship, company=r.company, status=r.status
            )
            for r in refs
        ],
        created_at=rc.created_at,
    )


async def list_reference_checks(
    db: AsyncSession, tenant_id: uuid.UUID, candidate_id: uuid.UUID
) -> list[PortalReferenceCheckOut]:
    notice = await ensure_notice(db, tenant_id, REFERENCE_PURPOSE)
    rows = (
        await db.execute(
            select(ReferenceCheck)
            .where(ReferenceCheck.candidate_id == candidate_id)
            .order_by(ReferenceCheck.created_at.desc())
        )
    ).scalars()
    return [await _ref_out(db, r, notice) for r in rows]


async def add_referees(
    ctx: RequestContext,
    db: AsyncSession,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    reference_check_id: uuid.UUID,
    data: RefereesIn,
) -> PortalReferenceCheckOut:
    from app.services import entity_refs
    from app.services.bgv import references
    from app.services.privacy.consent import grant_consent, has_active_consent

    rc = (
        await db.execute(
            select(ReferenceCheck)
            .where(ReferenceCheck.id == reference_check_id, ReferenceCheck.candidate_id == candidate_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if rc is None:
        raise NotFound("Reference check not found")
    if rc.status not in ("requested", "sent"):
        raise ConflictError("This reference check is closed", code="reference_closed")
    notice = await ensure_notice(db, tenant_id, REFERENCE_PURPOSE)
    if not (rc.consent_id and await has_active_consent(db, candidate_id, REFERENCE_PURPOSE)):
        if not data.agreed or data.consent_notice_id != notice.id:
            raise ConflictError("Agree to the reference-check notice first", code="reference_consent_missing")
        consent = await grant_consent(
            ctx,
            db,
            candidate_id,
            REFERENCE_PURPOSE,
            source="candidate_portal",
            notice_id=notice.id,
            evidence={
                "reference_check_id": str(rc.id),
                "ip_hash": _ip_hash(ctx),
                "ua": (ctx.user_agent or "")[:300],
            },
        )
        rc.consent_id = consent.id
    me = await entity_refs.addressee(db, "candidate", candidate_id)
    own = (me.address or "").lower() if me else ""
    existing = {
        str(e).lower()
        for (e,) in (await db.execute(select(Referee.email).where(Referee.reference_check_id == rc.id))).all()
    }
    errors: dict[str, str] = {}
    for i, r in enumerate(data.referees):
        email = str(r.email).lower()
        if own and email == own:
            errors[f"referees.{i}.email"] = "You cannot be your own referee"
        elif email in existing:
            errors[f"referees.{i}.email"] = "This referee is already listed"
        existing.add(email)
    if errors:
        raise ValidationFailed(
            "Please check the referees", code="invalid_referees", details={"fields": errors}
        )
    new = [
        Referee(
            id=uuid.uuid4(),
            tenant_id=rc.tenant_id,
            reference_check_id=rc.id,
            candidate_id=candidate_id,
            name=r.name,
            relationship=r.relationship,
            company=r.company,
            email=str(r.email).lower(),
            phone=r.phone,
            status="pending",
        )
        for r in data.referees
    ]
    db.add_all(new)
    await db.flush()
    await references.send_links(db, rc, new)
    return await _ref_out(db, rc, notice)
