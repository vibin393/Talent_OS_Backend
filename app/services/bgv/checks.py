# ruff: file-ignore[line-too-long]
"""Check processing (Epic 20 A5, P2-7.3, FR-112, AD-29): assign, start, record, waive, evidence, packet, overdue.

* **Start** needs an active ``bgv`` consent (409 ``bgv_consent_missing``; re-checked on every call). Internal
  assignee -> a P1 system Task. Provider assignee -> a ``connector_jobs`` row keyed
  ``bgv:initiate:bgv_check:<id>:<n>`` whose payload is exactly ``BgvCheckJob`` (data minimisation); with the
  **Manual** connector the domain issues the provider Secure Link itself (``provider_links``).
* **Record** (user, ``bgv:manage``) writes the result + evidence; ``discrepancy`` needs a discrepancy payload,
  created in the same transaction. A provider proposal is applied only through ``accept_proposal``.
* ★ **Waive** (``bgv:adjudicate``) is human-only: workflow / ai / system actors get 409 ``transition_not_allowed``.
"""

import io
import json
import uuid
import zipfile

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.models.bgv import CASE_TERMINAL, BgvCase, BgvCheck, BgvEvidence, BgvProvider
from app.schemas.bgv import AssignIn, CheckOut, ResultIn, WaiveIn
from app.services.bgv import status as st
from app.services.bgv.cases import check_out
from app.services.bgv.common import (
    UPLOAD_KINDS,
    actor,
    add_working_days,
    get_case,
    get_check,
    now,
    record_event,
    require_consent,
    today,
    user_id,
)
from app.services.connectors.bgv.port import BgvCheckJob

_ACTIVE_CASE = ("documents_pending", "in_verification", "discrepancy", "under_review")


def _case_open(case: BgvCase) -> None:
    if case.status not in _ACTIVE_CASE:
        raise ConflictError(
            "Checks can be worked only once the case is started and until it is decided",
            code="case_not_active",
            details={"status": case.status},
        )


async def assign(ctx: RequestContext, db: AsyncSession, check_id: uuid.UUID, data: AssignIn) -> CheckOut:
    from app.models.identity import User

    check = await get_check(db, check_id, lock=True)
    case = await get_case(db, check.case_id)
    if case.status in CASE_TERMINAL:
        raise ConflictError("The case is closed", code="case_closed")
    if check.status not in ("pending", "in_progress"):
        raise ConflictError("Only open checks can be reassigned", code="check_closed")
    if data.assignee_kind == "internal":
        user = await db.get(User, data.assignee_user_id) if data.assignee_user_id else None
        if user is None or not user.is_active:
            raise ValidationFailed(
                "Choose an active user", details={"fields": {"assignee_user_id": "Unknown user"}}
            )
        check.assignee_user_id, check.provider_id = user.id, None
    else:
        provider = await db.get(BgvProvider, data.provider_id) if data.provider_id else None
        if provider is None or not provider.active:
            raise ValidationFailed(
                "Choose an active provider", details={"fields": {"provider_id": "Unknown provider"}}
            )
        if provider.supported_types and check.type not in provider.supported_types:
            raise ValidationFailed(
                "The provider does not run this check type", code="provider_type_unsupported"
            )
        check.provider_id, check.assignee_user_id = provider.id, None
    check.assignee_kind = data.assignee_kind
    record_event(db, case, "check_assigned", actor(ctx), check_id=check.id, assignee_kind=data.assignee_kind)
    await db.flush()
    return await check_out(db, check)


async def build_job(db: AsyncSession, check: BgvCheck) -> BgvCheckJob:
    from app.services.bgv.provider_links import check_file_ids

    files, declarations = await check_file_ids(db, check)
    return BgvCheckJob(
        check_id=check.id,
        check_type=check.type,
        subject_label=check.subject_label,
        file_ids=tuple(files),
        declarations=declarations,
    )


async def _legal_entity(db: AsyncSession, case: BgvCase) -> uuid.UUID | None:
    from app.models.placements import Placement

    if case.placement_id is None:
        return None
    p = await db.get(Placement, case.placement_id)
    return p.legal_entity_id if p is not None else None


async def start(ctx: RequestContext, db: AsyncSession, check_id: uuid.UUID) -> CheckOut:
    from app.schemas.tasks import TaskCreate
    from app.services import tasks_service
    from app.services.bgv import provider_links
    from app.services.connectors import jobs
    from app.services.connectors import registry as connectors

    check = await get_check(db, check_id, lock=True)
    case = await get_case(db, check.case_id, lock=True)
    _case_open(case)
    await require_consent(db, case)
    if check.status != "pending" and not (check.status == "in_progress" and check.halted_reason):
        raise ConflictError("The check is already started or finished", code="check_already_started")
    if check.assignee_kind is None:
        raise ConflictError("Assign the check before starting it", code="check_unassigned")
    check.status = "in_progress"
    check.halted_reason = None
    check.started_at = now()
    check.overdue = False
    check.due_on = add_working_days(today(), check.turnaround_days or 5)
    check.start_count = (check.start_count or 0) + 1
    if check.assignee_kind == "internal" and check.assignee_user_id:
        from datetime import UTC, datetime, time

        task = await tasks_service.create_system_task(
            ctx,
            db,
            TaskCreate(
                title=f"BGV {case.human_id}: verify {check.subject_label}",
                owner_id=check.assignee_user_id,
                entity_type="candidate",
                entity_id=case.candidate_id,
                due_at=datetime.combine(check.due_on, time(18, 0), tzinfo=UTC),
                priority="high" if case.required else "normal",
            ),
            source="bgv",
        )
        check.task_id = task.id
    elif check.assignee_kind == "provider" and check.provider_id:
        provider = await db.get(BgvProvider, check.provider_id)
        if provider is None:
            raise NotFound("BGV provider not found")
        job = await build_job(db, check)
        le_id = await _legal_entity(db, case)
        queued = await jobs.enqueue(
            ctx, db, "bgv", "initiate", "bgv_check", check.id, check.start_count, job.to_payload()
        )
        check.connector_job_id = queued.id
        adapter = await connectors.get(ctx, db, "bgv", le_id)
        if adapter.provider_key == "manual":
            await provider_links.issue(db, check, provider, user_id(ctx))
    record_event(db, case, "check_started", actor(ctx), check_id=check.id, due_on=check.due_on)
    await db.flush()
    await st.recompute_case_status(db, case)
    return await check_out(db, check)


async def _attach_evidence(
    db: AsyncSession, check: BgvCheck, case: BgvCase, file_ids: list[uuid.UUID], kind: str
) -> None:
    from app.models.files import StoredFile

    existing = {
        f
        for (f,) in (
            await db.execute(select(BgvEvidence.file_id).where(BgvEvidence.check_id == check.id))
        ).all()
    }
    for fid in file_ids:
        if fid in existing:
            continue
        row = await db.get(StoredFile, fid)
        if row is None or row.deleted_at is not None:
            raise ValidationFailed(
                "Unknown evidence file", details={"fields": {"evidence_file_ids": str(fid)}}
            )
        row.owner_entity_type, row.owner_entity_id = "bgv_case", case.id
        db.add(
            BgvEvidence(
                id=uuid.uuid4(),
                tenant_id=case.tenant_id,
                check_id=check.id,
                candidate_id=case.candidate_id,
                file_id=fid,
                uploaded_by_kind=kind,
                uploaded_at=now(),
            )
        )
    await db.flush()


async def record_result(
    ctx: RequestContext, db: AsyncSession, check_id: uuid.UUID, data: ResultIn
) -> CheckOut:
    from app.core import secure_links
    from app.core.identity_docs import assert_no_full_aadhaar
    from app.services.bgv import discrepancies
    from app.services.bgv.provider_links import PURPOSE

    who = actor(ctx)
    if who.kind != "user":
        raise st.not_allowed("in_progress", data.result, who.kind)
    assert_no_full_aadhaar(data.note, data.verifier_name, data.source_contacted)
    check = await get_check(db, check_id, lock=True)
    case = await get_case(db, check.case_id, lock=True)
    _case_open(case)
    await require_consent(db, case)
    if check.status != "in_progress":
        raise ConflictError("Only a check in progress can be recorded", code="check_not_in_progress")
    values = data.model_dump()
    if data.accept_proposal:
        proposal = check.provider_proposal
        if not proposal:
            raise ConflictError("There is no provider proposal to accept", code="no_proposal")
        for key in ("note", "verifier_name", "source_contacted", "verified_on"):
            if values.get(key) in (None, ""):
                values[key] = proposal.get(key)
    if data.result == "discrepancy" and data.discrepancy is None:
        raise ValidationFailed(
            "Describe the discrepancy",
            code="discrepancy_required",
            details={"fields": {"discrepancy": "Required"}},
        )
    check.status = data.result
    check.result_note = values["note"]
    check.verifier_name = values["verifier_name"]
    check.source_contacted = values["source_contacted"]
    verified_on = values["verified_on"]
    if isinstance(verified_on, str):
        from datetime import date

        verified_on = date.fromisoformat(verified_on)
    check.verified_on = verified_on
    check.recorded_by = user_id(ctx)
    check.recorded_at = now()
    check.overdue = False
    await _attach_evidence(db, check, case, list(data.evidence_file_ids), "user")
    await secure_links.revoke_for_subject(db, "bgv_check", check.id, PURPOSE)
    if data.discrepancy is not None and data.result == "discrepancy":
        await discrepancies.raise_discrepancy(ctx, db, check, case, data.discrepancy)
    record_event(
        db,
        case,
        "check_recorded",
        who,
        check_id=check.id,
        result=data.result,
        accepted_proposal=data.accept_proposal,
    )
    await db.flush()
    await st.recompute_case_status(db, case)
    return await check_out(db, check)


async def waive(ctx: RequestContext, db: AsyncSession, check_id: uuid.UUID, data: WaiveIn) -> CheckOut:
    """★ human-only (FR-181)."""
    who = actor(ctx)
    check = await get_check(db, check_id, lock=True)
    if who.kind != "user":
        raise st.not_allowed(check.status, "waived", who.kind)
    case = await get_case(db, check.case_id, lock=True)
    _case_open(case)
    if check.status not in ("pending", "in_progress", "unable_to_verify"):
        raise ConflictError("This check cannot be waived", code="check_not_waivable")
    check.status = "waived"
    check.waived_reason = data.reason
    check.recorded_by = user_id(ctx)
    check.recorded_at = now()
    check.overdue = False
    record_event(db, case, "check_waived", who, check_id=check.id)
    audit.record("bgv.check_waived", "bgv_check", check.id, session=db)
    await db.flush()
    await st.recompute_case_status(db, case)
    return await check_out(db, check)


async def upload_evidence(
    ctx: RequestContext, db: AsyncSession, check_id: uuid.UUID, content: bytes, filename: str | None
) -> CheckOut:
    from app.services import files_service

    check = await get_check(db, check_id, lock=True)
    case = await get_case(db, check.case_id)
    _case_open(case)
    stored = await files_service.store_upload(ctx, db, content, filename, UPLOAD_KINDS, "bgv_case", case.id)
    await _attach_evidence(db, check, case, [stored.id], "user")
    return await check_out(db, check)


async def packet(ctx: RequestContext, db: AsyncSession, check_id: uuid.UUID) -> tuple[bytes, str]:  # ruff: ignore[unused-function-argument]
    """Build the minimal check packet (ZIP: the check's own files + ``declarations.json``), audited."""
    from app.services import files_service

    check = await get_check(db, check_id)
    case = await get_case(db, check.case_id)
    job = await build_job(db, check)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "check.json",
            json.dumps({"check_type": job.check_type, "subject_label": job.subject_label}, indent=2),
        )
        zf.writestr("declarations.json", json.dumps(dict(job.declarations), indent=2, default=str))
        for i, fid in enumerate(job.file_ids, start=1):
            row = await files_service.get_file(db, fid)
            if row is None or row.deleted_at is not None:
                continue
            try:
                data = await files_service.read_file(row)
            except FileNotFoundError:
                continue
            zf.writestr(f"{i:02d}_{row.original_name}", data)
    audit.record("bgv.packet_downloaded", "bgv_check", check.id, session=db, files=len(job.file_ids))
    return buf.getvalue(), f"{case.human_id}-{check.type}-{check.seq}.zip"


async def mark_overdue(db: AsyncSession) -> int:
    """Daily beat ``bgv.overdue``: in-progress checks past ``due_on`` -> ``overdue`` + notification."""
    from app.services.bgv.common import compliance_recipients
    from app.services.bgv.events import BgvCheckOverdue

    rows = list(
        (
            await db.execute(
                select(BgvCheck).where(
                    BgvCheck.status == "in_progress", BgvCheck.overdue.is_(False), BgvCheck.due_on < today()
                )
            )
        ).scalars()
    )
    if not rows:
        return 0
    compliance = await compliance_recipients(db)
    for c in rows:
        c.overdue = True
        recipients = {*compliance, *([c.assignee_user_id] if c.assignee_user_id else [])}
        emit(
            db,
            BgvCheckOverdue(
                tenant_id=c.tenant_id, check_id=c.id, case_id=c.case_id, recipient_ids=tuple(recipients)
            ),
        )
    await db.flush()
    return len(rows)
