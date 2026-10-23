# ruff: file-ignore[line-too-long]
"""Worker "My Work" immigration (Epic 46 A3, AD-26). Runs in the worker org scope (``immigration_cases``,
``case_document_requests`` and ``permits`` of other Workers are invisible) and checks ownership explicitly
(another Worker's case or request is a 404). Output: ``schemas/immigration_portal.py`` only -- no owner remarks,
counsel details, numbers or outcome notes.

The ``immigration`` notice is shown before the first upload; uploads need an active ``immigration`` consent
(409 ``consent_required``), recorded with source ``worker_portal``.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.immigration import (
    CaseDependant,
    CaseDocumentRequest,
    ImmigrationCase,
    VisaType,
    VisaTypeVersion,
)
from app.schemas.immigration import VisaTypeDefinition
from app.schemas.immigration_portal import (
    PortalCaseDetailOut,
    PortalCaseOut,
    PortalCasesOut,
    PortalNoticeOut,
    PortalRequestOut,
)
from app.services.immigration import checklist, transitions
from app.services.immigration import definition as defs
from app.services.immigration.common import CONSENT_PURPOSE, conflict, not_found, tenant_of

OPEN_REQUEST = ("requested", "rejected")
PLAIN_STATUS = {
    "open": "We are preparing your application",
    "in_stage": "Your application is in progress",
    "approved": "Approved",
    "refused": "Not approved",
    "withdrawn": "Withdrawn",
}


async def notice(db: AsyncSession) -> PortalNoticeOut:
    from app.services.privacy import consent

    row = None
    try:
        async with db.begin_nested():
            row = await consent.active_notice(db, tenant_of(db), CONSENT_PURPOSE)
    except Exception:  # a worker session may not seed notices: fall back to the registered default text
        row = None
    if row is not None:
        return PortalNoticeOut(notice_id=row.id, version=row.version, title=row.title, body_md=row.body_md)
    title, body = consent.DEFAULT_NOTICES.get(CONSENT_PURPOSE, ("Immigration documents", ""))
    return PortalNoticeOut(notice_id=None, version=None, title=title, body_md=body)


async def _consented(db: AsyncSession, candidate_id: uuid.UUID) -> bool:
    from app.services.privacy import consent

    return await consent.has_active_consent(db, candidate_id, CONSENT_PURPOSE)


async def _defs(
    db: AsyncSession, cases: list[ImmigrationCase]
) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, VisaTypeDefinition]]:
    names: dict[uuid.UUID, str] = {}
    versions: dict[uuid.UUID, VisaTypeDefinition] = {}
    if not cases:
        return names, versions
    for vt in (
        await db.execute(select(VisaType).where(VisaType.id.in_({c.visa_type_id for c in cases})))
    ).scalars():
        names[vt.id] = vt.name
    for v in (
        await db.execute(
            select(VisaTypeVersion).where(VisaTypeVersion.id.in_({c.visa_type_version_id for c in cases}))
        )
    ).scalars():
        versions[v.id] = VisaTypeDefinition.model_validate(v.definition)
    return names, versions


async def _requests(
    db: AsyncSession, candidate_id: uuid.UUID, case_ids: list[uuid.UUID]
) -> list[CaseDocumentRequest]:
    if not case_ids:
        return []
    return list(
        (
            await db.execute(
                select(CaseDocumentRequest)
                .where(
                    CaseDocumentRequest.candidate_id == candidate_id,
                    CaseDocumentRequest.case_id.in_(case_ids),
                    CaseDocumentRequest.collect_from == "worker",
                    CaseDocumentRequest.requested_at.is_not(None),
                )
                .order_by(CaseDocumentRequest.created_at, CaseDocumentRequest.label)
            )
        ).scalars()
    )


def _case_out(
    c: ImmigrationCase,
    names: dict[uuid.UUID, str],
    versions: dict[uuid.UUID, VisaTypeDefinition],
    reqs: list[CaseDocumentRequest],
) -> dict[str, Any]:
    d = versions.get(c.visa_type_version_id)
    st = defs.stage(d, c.stage_key) if d is not None else None
    return {
        "id": c.id,
        "reference": c.human_id,
        "country": c.country,
        "visa_type_name": names.get(c.visa_type_id, ""),
        "status": c.status,
        "status_label": PLAIN_STATUS.get(c.status, transitions.STATUS_LABELS.get(c.status, c.status)),
        "stage_label": st.label if st is not None and c.status == "in_stage" else None,
        "open_requests": sum(1 for r in reqs if r.case_id == c.id and r.status in OPEN_REQUEST),
        "updated_at": c.updated_at,
    }


async def _own_cases(db: AsyncSession, candidate_id: uuid.UUID) -> list[ImmigrationCase]:
    return list(
        (
            await db.execute(
                select(ImmigrationCase)
                .where(ImmigrationCase.candidate_id == candidate_id)
                .order_by(ImmigrationCase.created_at.desc())
            )
        ).scalars()
    )


async def list_mine(db: AsyncSession, candidate_id: uuid.UUID) -> PortalCasesOut:
    cases = await _own_cases(db, candidate_id)
    names, versions = await _defs(db, cases)
    reqs = await _requests(db, candidate_id, [c.id for c in cases])
    return PortalCasesOut(
        cases=[PortalCaseOut(**_case_out(c, names, versions, reqs)) for c in cases],
        consent_given=await _consented(db, candidate_id),
        notice=await notice(db),
    )


async def _own_case(db: AsyncSession, candidate_id: uuid.UUID, case_id: uuid.UUID) -> ImmigrationCase:
    c = await db.get(ImmigrationCase, case_id)
    if c is None or c.candidate_id != candidate_id:
        raise not_found()
    return c


def _request_out(
    r: CaseDocumentRequest, dep_names: dict[uuid.UUID, str], case_open: bool
) -> PortalRequestOut:
    return PortalRequestOut(
        id=r.id,
        label=r.label,
        for_dependant=dep_names.get(r.dependant_id) if r.dependant_id else None,
        mandatory=r.mandatory,
        status=r.status,
        rejection_reason=r.rejection_reason if r.status == "rejected" else None,
        due_date=r.due_date,
        can_upload=case_open and r.status in ("requested", "rejected", "uploaded"),
    )


async def _dep_names(db: AsyncSession, case_id: uuid.UUID) -> dict[uuid.UUID, str]:
    """Dependants are internal-only rows; the Worker sees the first name of their own dependants on labels."""
    try:
        rows = await db.execute(
            select(CaseDependant.id, CaseDependant.name).where(CaseDependant.case_id == case_id)
        )
        return {i: n.split(" ")[0] for i, n in rows.tuples().all()}
    except Exception:  # pragma: no cover
        return {}


async def detail(db: AsyncSession, candidate_id: uuid.UUID, case_id: uuid.UUID) -> PortalCaseDetailOut:
    c = await _own_case(db, candidate_id, case_id)
    names, versions = await _defs(db, [c])
    reqs = await _requests(db, candidate_id, [c.id])
    deps = await _dep_names(db, c.id)
    open_case = c.status in ("open", "in_stage", "approved")
    return PortalCaseDetailOut(
        **_case_out(c, names, versions, reqs), requests=[_request_out(r, deps, open_case) for r in reqs]
    )


async def give_consent(db: AsyncSession, candidate_id: uuid.UUID) -> str:
    from app.services.privacy import consent

    n = await notice(db)
    await consent.record(db, candidate_id, CONSENT_PURPOSE, "worker_portal", n.version, None)
    return "granted"


async def upload(
    db: AsyncSession, candidate_id: uuid.UUID, request_id: uuid.UUID, data: bytes, filename: str | None
) -> PortalRequestOut:
    req = await db.get(CaseDocumentRequest, request_id)
    if (
        req is None
        or req.candidate_id != candidate_id
        or req.collect_from != "worker"
        or req.requested_at is None
    ):
        raise not_found("Document request")
    if req.status not in ("requested", "rejected", "uploaded"):
        raise conflict("request_closed", "This item is already complete")
    if not await _consented(db, candidate_id):
        raise conflict(
            "consent_required",
            "Read and accept the immigration notice before uploading",
            purpose=CONSENT_PURPOSE,
        )
    await checklist.store_upload(
        db, req, data, filename, actor={"kind": "candidate", "id": str(candidate_id)}, via="worker_portal"
    )
    return _request_out(req, await _dep_names(db, req.case_id), True)
