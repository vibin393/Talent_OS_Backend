# ruff: file-ignore[unused-function-argument]
"""Onboarding task operations (Epic 23 A5 / A6): drafts, submission, uploads, policy acknowledgement, eSign
requests, and the handlers of connector / sibling events (signature, credential, P1 task, bank approval).

Every status change goes through ``status.check_task`` (AD-4) and ends with a case ``recompute``.
"""

import hashlib
import uuid
from collections.abc import Mapping
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.events.onboarding import PolicyAcknowledged
from app.models.onboarding import OnboardingAcknowledgement, OnboardingCase, OnboardingTask
from app.services.onboarding import task_kinds
from app.services.onboarding.common import (
    DONE_TASK_STATUSES,
    TERMINAL_CASE,
    WORKER_OPEN,
    as_uuid,
    assert_no_full_aadhaar,
    assert_no_sensitive_keys,
    get_settings_row,
    now,
    scrub_draft,
    tenant_of,
)
from app.services.onboarding.status import check_task

MAX_UPLOAD = 10 * 1024 * 1024
UPLOAD_KINDS = ("pdf", "jpeg", "png", "heic", "webp")


def system_ctx(db: AsyncSession) -> RequestContext:
    return RequestContext(tenant_id=tenant_of(db), actor_kind="system")


async def _case(db: AsyncSession, task: OnboardingTask, *, for_update: bool = True) -> OnboardingCase:
    stmt = select(OnboardingCase).where(OnboardingCase.id == task.case_id)
    if for_update:
        stmt = stmt.with_for_update()
    case = (await db.execute(stmt)).scalar_one()
    return case


def _open_case(case: OnboardingCase) -> None:
    if case.status in TERMINAL_CASE and case.status == "cancelled":
        raise ConflictError("This onboarding case was cancelled", code="case_closed")


async def decide(
    ctx: RequestContext,
    db: AsyncSession,
    task: OnboardingTask,
    to_status: str,
    actor: str,
    *,
    decided_by: uuid.UUID | None = None,
    comment: str | None = None,
    reason: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> None:
    """Apply one task transition (approve runs the kind's ``on_approve`` in this transaction)."""
    from app.services.onboarding.cases import recompute

    check_task(task.status, to_status, actor)
    if to_status == "approved":
        await task_kinds.on_approve(task.kind)(ctx, db, task, dict(extra or {}))
        task.return_comment = None
    elif to_status == "returned":
        task.return_comment = comment
    elif to_status == "waived":
        task.waived_reason = reason
    if to_status in ("approved", "waived", "returned"):
        task.decided_by_kind = "user" if decided_by is not None else "system"
        task.decided_by_id = decided_by
        task.decided_at = now()
    previous = task.status
    task.status = to_status
    await db.flush()
    audit.record(
        f"onboarding.task_{to_status}",
        "onboarding_task",
        task.id,
        session=db,
        kind=task.kind,
        from_status=previous,
        actor=actor,
    )
    case = await _case(db, task)
    if to_status == "returned":
        from app.events.onboarding import OnboardingTaskReturned
        from app.services.onboarding import notify

        emit(
            db,
            OnboardingTaskReturned(
                tenant_id=task.tenant_id,
                task_id=task.id,
                case_id=task.case_id,
                candidate_id=task.candidate_id,
            ),
        )
        await notify.to_worker(
            db,
            notify.TASK_RETURNED,
            task.candidate_id,
            f"Please update your onboarding task: {task.label}",
            comment,
        )
    await recompute(ctx, db, case)


# ---------------------------------------------------------------- drafts / submission (Worker)
async def save_draft(
    ctx: RequestContext, db: AsyncSession, task: OnboardingTask, draft: Mapping[str, Any]
) -> None:
    if task.assignee != "worker":
        raise NotFound("Onboarding task not found")
    if task.status not in WORKER_OPEN:
        raise ConflictError("This task is not open", code="task_not_open")
    assert_no_full_aadhaar(dict(draft))
    task.draft = scrub_draft(dict(draft))
    assert_no_sensitive_keys(task.draft)
    await db.flush()


async def submit(
    ctx: RequestContext,
    db: AsyncSession,
    task: OnboardingTask,
    payload: Mapping[str, Any],
    *,
    actor: str = "candidate",
) -> None:
    if task.assignee != "worker":
        raise NotFound("Onboarding task not found")
    case = await _case(db, task)
    if case.status in TERMINAL_CASE:
        raise ConflictError("This onboarding case is closed", code="case_closed")
    if task.status not in WORKER_OPEN:
        raise ConflictError("This task is not open", code="task_not_open")
    body = dict(payload)
    assert_no_full_aadhaar(body)
    handler = task_kinds.SUBMIT.get(task.kind)
    if handler is None:
        raise ConflictError("This task is not completed by the Worker", code="not_submittable")
    await handler(ctx, db, case, task, body)
    assert_no_sensitive_keys(task.data)
    check_task(task.status, "submitted", actor)
    task.status = "submitted"
    task.submitted_at = now()
    task.draft = None
    await db.flush()
    audit.record("onboarding.task_submitted", "onboarding_task", task.id, session=db, kind=task.kind)
    if task.auto_approve and not (task.kind == "bank_details" and (task.data or {}).get("change_requested")):
        try:
            async with db.begin_nested():
                await decide(ctx, db, task, "approved", "system")
                return
        except Exception:
            logger.info("auto-approve of onboarding task {} deferred to review", task.id)
    from app.services.onboarding.cases import recompute

    await recompute(ctx, db, case)


async def upload(
    ctx: RequestContext,
    db: AsyncSession,
    task: OnboardingTask,
    data: bytes,
    filename: str | None,
) -> uuid.UUID:
    from app.services import documents_service

    if task.assignee != "worker" or not task_kinds.uploadable(task):
        raise ConflictError("This task does not take files", code="not_uploadable")
    if task.status not in WORKER_OPEN:
        raise ConflictError("This task is not open", code="task_not_open")
    if len(data) > MAX_UPLOAD:
        raise ValidationFailed("The file is larger than 10 MB", code="file_too_large")
    doc = await documents_service.create_document(
        system_ctx(db),
        db,
        owner_kind="candidate",
        owner_id=task.candidate_id,
        data=data,
        filename=filename,
        category=task_kinds.upload_category(task),
        title=f"{task.label}"[:300],
        access_level="hr",
    )
    task.document_ids = [*(task.document_ids or []), doc.id]
    await db.flush()
    return doc.id


# ---------------------------------------------------------------- policy acknowledgement
async def _render_policy(db: AsyncSession, task: OnboardingTask) -> tuple[bytes, uuid.UUID, uuid.UUID]:
    from app.services import documents_service

    template_id = as_uuid((task.config or {}).get("document_template_id"))
    if template_id is None:
        raise NotFound("Policy not found")
    case = await _case(db, task, for_update=False)
    pdf, template, version = await documents_service.templates.render_template(
        db, template_id, merge_refs={"placement": case.placement_id}, merge_data=None
    )
    return pdf, template.id, version.id


async def policy_pdf(ctx: RequestContext, db: AsyncSession, task: OnboardingTask) -> bytes:
    if task.kind != "policy_ack":
        raise NotFound("Policy not found")
    pdf, _, _ = await _render_policy(db, task)
    return pdf


async def acknowledge(
    ctx: RequestContext,
    db: AsyncSession,
    task: OnboardingTask,
    *,
    accepted: bool,
    ip: str | None,
    user_agent: str | None,
) -> None:
    if task.kind != "policy_ack" or task.assignee != "worker":
        raise NotFound("Onboarding task not found")
    if not accepted:
        raise ValidationFailed("Tick 'I have read and accept' to continue", code="acceptance_required")
    case = await _case(db, task)
    if case.status == "cancelled":
        raise ConflictError("This onboarding case is closed", code="case_closed")
    if task.status not in WORKER_OPEN:
        raise ConflictError("This task is not open", code="task_not_open")
    pdf, template_id, version_id = await _render_policy(db, task)
    db.add(
        OnboardingAcknowledgement(
            tenant_id=task.tenant_id,
            task_id=task.id,
            candidate_id=task.candidate_id,
            document_template_id=template_id,
            template_version_id=version_id,
            rendered_sha256=hashlib.sha256(pdf).hexdigest(),
            acknowledged_at=now(),
            ip=ip,
            user_agent=(user_agent or "")[:400] or None,
        )
    )
    task.data = {"template_version_id": str(version_id), "acknowledged_at": now().isoformat()}
    check_task(task.status, "submitted", "candidate")
    task.status = "submitted"
    task.submitted_at = now()
    await db.flush()
    await decide(ctx, db, task, "approved", "system")
    emit(
        db,
        PolicyAcknowledged(
            tenant_id=task.tenant_id,
            candidate_id=task.candidate_id,
            template_id=template_id,
            template_version_id=version_id,
        ),
    )


# ---------------------------------------------------------------- eSign
async def _wants_tenant_signatory(db: AsyncSession, template_id: uuid.UUID) -> bool:
    from app.services import documents_service

    try:
        t = await documents_service.templates.get_template_row(db, template_id)
    except Exception:
        return False
    return any((r or {}).get("role") == "tenant_signatory" for r in (t.signer_roles or []))


async def ensure_signature_request(
    db: AsyncSession, case: OnboardingCase, task: OnboardingTask
) -> uuid.UUID | None:
    from app.schemas.documents import SignatureRequestCreateIn, SignerSpec
    from app.services import documents_service

    if task.kind != "esign" or task.signature_request_id is not None or task.status not in WORKER_OPEN:
        return task.signature_request_id
    template_id = as_uuid((task.config or {}).get("document_template_id"))
    if template_id is None:
        return None
    signers = [SignerSpec(kind="worker", ref_id=task.candidate_id, email="", order=1)]
    settings = await get_settings_row(db)
    if settings.default_tenant_signatory_user_id and await _wants_tenant_signatory(db, template_id):
        signers.append(
            SignerSpec(
                kind="tenant_signatory", ref_id=settings.default_tenant_signatory_user_id, email="", order=2
            )
        )
    summary = await documents_service.create_signature_request(
        system_ctx(db),
        db,
        SignatureRequestCreateIn(
            template_id=template_id,
            merge_refs={"placement": case.placement_id},
            owner_kind="placement",
            owner_id=case.placement_id,
            subject_kind="onboarding_task",
            subject_id=task.id,
            signers=signers,
            send=True,
        ),
    )
    task.signature_request_id = summary.id
    await db.flush()
    return summary.id


async def prepare_worker_task(
    ctx: RequestContext, db: AsyncSession, case: OnboardingCase, task: OnboardingTask
) -> None:
    if task.kind == "esign" and task.signature_request_id is None and task.status in WORKER_OPEN:
        try:
            async with db.begin_nested():
                await ensure_signature_request(db, case, task)
        except Exception:
            logger.exception("could not create the signature request of onboarding task {}", task.id)


async def worker_signer_id(db: AsyncSession, task: OnboardingTask) -> uuid.UUID | None:
    from app.models.documents import Signer

    if task.signature_request_id is None:
        return None
    return (
        await db.execute(
            select(Signer.id).where(
                Signer.request_id == task.signature_request_id,
                Signer.kind == "candidate",
                Signer.principal_id == task.candidate_id,
            )
        )
    ).scalar_one_or_none()


# ---------------------------------------------------------------- event handlers (system)
async def _task_by_id(db: AsyncSession, task_id: Any) -> OnboardingTask | None:
    tid = as_uuid(task_id)
    if tid is None:
        return None
    return (
        await db.execute(select(OnboardingTask).where(OnboardingTask.id == tid).with_for_update())
    ).scalar_one_or_none()


async def _active(db: AsyncSession, task: OnboardingTask) -> bool:
    case = await _case(db, task, for_update=False)
    return case.status != "cancelled"


async def on_signature_completed(ctx: RequestContext, db: AsyncSession, payload: Mapping[str, Any]) -> bool:
    if payload.get("subject_kind") != "onboarding_task":
        return False
    task = await _task_by_id(db, payload.get("subject_id"))
    if task is None or task.status in DONE_TASK_STATUSES or not await _active(db, task):
        return False
    if str(task.signature_request_id) != str(payload.get("request_id")):
        return False
    doc = as_uuid(payload.get("document_id"))
    if doc and doc not in (task.document_ids or []):
        task.document_ids = [*(task.document_ids or []), doc]
    task.data = {**(task.data or {}), "signed": True}
    task.submitted_at = task.submitted_at or now()
    await decide(ctx, db, task, "approved", "system")
    return True


async def on_signature_status_changed(
    ctx: RequestContext, db: AsyncSession, payload: Mapping[str, Any]
) -> bool:
    to_status = str(payload.get("to_status") or "")
    if payload.get("subject_kind") != "onboarding_task" or to_status not in (
        "declined",
        "expired",
        "cancelled",
    ):
        return False
    task = await _task_by_id(db, payload.get("subject_id"))
    if task is None or task.status not in ("pending", "submitted") or not await _active(db, task):
        return False
    if str(task.signature_request_id) != str(payload.get("request_id")):
        return False
    await decide(ctx, db, task, "returned", "system", comment=f"Signature {to_status}")
    return True


async def reissue_signature(ctx: RequestContext, db: AsyncSession, task: OnboardingTask) -> uuid.UUID | None:
    if task.kind != "esign":
        raise ConflictError("Only eSign tasks have signatures", code="not_esign")
    if task.status != "returned":
        raise ConflictError("Only a returned eSign task can be re-issued", code="task_not_returned")
    case = await _case(db, task)
    if case.status in TERMINAL_CASE:
        raise ConflictError("This onboarding case is closed", code="case_closed")
    task.signature_request_id = None
    await db.flush()
    rid = await ensure_signature_request(db, case, task)
    audit.record("onboarding.signature_reissued", "onboarding_task", task.id, session=db)
    del ctx
    return rid


async def on_credential_recorded(ctx: RequestContext, db: AsyncSession, payload: Mapping[str, Any]) -> int:
    cid, key = as_uuid(payload.get("candidate_id")), str(payload.get("type_key") or "")
    if cid is None or not key:
        return 0
    rows = (
        await db.execute(
            select(OnboardingTask)
            .join(OnboardingCase, OnboardingCase.id == OnboardingTask.case_id)
            .where(
                OnboardingTask.candidate_id == cid,
                OnboardingTask.kind == "credential",
                OnboardingTask.status.in_(WORKER_OPEN),
                OnboardingCase.status.not_in(TERMINAL_CASE),
            )
            .with_for_update(of=OnboardingTask)
        )
    ).scalars()
    n = 0
    for task in list(rows):
        if str((task.config or {}).get("credential_type_key")) != key:
            continue
        task.credential_id = as_uuid(payload.get("credential_id"))
        check_task(task.status, "submitted", "system")
        task.status = "submitted"
        task.submitted_at = now()
        task.data = {"credential_id": str(task.credential_id)}
        await db.flush()
        if task.credential_id is not None and await _credential_is_valid(db, task.credential_id):
            await decide(ctx, db, task, "approved", "system")  # already Valid (e.g. recorded by compliance)
        else:
            from app.services.onboarding.cases import recompute

            await recompute(ctx, db, await _case(db, task))
        n += 1
    return n


async def _credential_is_valid(db: AsyncSession, credential_id: uuid.UUID) -> bool:
    from sqlalchemy import text

    row = (
        await db.execute(
            text("SELECT verification_status FROM credentials WHERE id = :id"), {"id": credential_id}
        )
    ).first()
    return row is not None and row[0] == "valid"


async def _link_open_tasks(ctx: RequestContext, db: AsyncSession, payload: Mapping[str, Any]) -> int:
    """Link a credential verified Valid to the worker's open, unlinked ``credential`` tasks and approve them.

    Covers a credential recorded before the case and verified after the task was created.
    """
    from app.services.onboarding import reuse

    cid, key = as_uuid(payload.get("candidate_id")), str(payload.get("type_key") or "")
    if cid is None or not key or await reuse.valid_credential_id(db, cid, key) is None:
        return 0
    rows = (
        await db.execute(
            select(OnboardingTask)
            .join(OnboardingCase, OnboardingCase.id == OnboardingTask.case_id)
            .where(
                OnboardingTask.candidate_id == cid,
                OnboardingTask.kind == "credential",
                OnboardingTask.status.in_(WORKER_OPEN),
                OnboardingCase.status.not_in(TERMINAL_CASE),
            )
            .with_for_update(of=OnboardingTask)
        )
    ).scalars()
    n = 0
    for task in list(rows):
        if str((task.config or {}).get("credential_type_key")) != key:
            continue
        task.credential_id = as_uuid(payload.get("credential_id"))
        task.data = {"credential_id": str(task.credential_id)}
        check_task(task.status, "submitted", "system")
        task.status = "submitted"
        task.submitted_at = now()
        await db.flush()
        await decide(ctx, db, task, "approved", "system")
        n += 1
    return n


async def on_credential_verified(ctx: RequestContext, db: AsyncSession, payload: Mapping[str, Any]) -> int:
    credential_id = as_uuid(payload.get("credential_id"))
    verdict = str(payload.get("verification_status") or "")
    if credential_id is None:
        return 0
    rows = list(
        (
            await db.execute(
                select(OnboardingTask)
                .where(OnboardingTask.credential_id == credential_id, OnboardingTask.status == "submitted")
                .with_for_update()
            )
        ).scalars()
    )
    n = 0
    for task in rows:
        if not await _active(db, task):
            continue
        if verdict in ("valid", "verified"):
            await decide(ctx, db, task, "approved", "system")
        elif verdict in ("rejected", "invalid"):
            await decide(
                ctx, db, task, "returned", "system", comment="The credential was rejected by compliance"
            )
        else:
            continue
        n += 1
    if verdict == "valid":
        n += await _link_open_tasks(ctx, db, payload)
    return n


async def on_p1_task_completed(ctx: RequestContext, db: AsyncSession, payload: Mapping[str, Any]) -> bool:
    p1 = as_uuid(payload.get("task_id"))
    if p1 is None:
        return False
    task = (
        await db.execute(select(OnboardingTask).where(OnboardingTask.p1_task_id == p1).with_for_update())
    ).scalar_one_or_none()
    if task is None or task.status != "pending" or not await _active(db, task):
        return False
    task.submitted_at = now()
    await decide(ctx, db, task, "approved", "system")
    completed_by = as_uuid(payload.get("completed_by"))
    if completed_by is not None:
        task.decided_by_kind = "user"
        task.decided_by_id = completed_by
        await db.flush()
    return True


async def on_bank_change_decided(ctx: RequestContext, db: AsyncSession, payload: Mapping[str, Any]) -> bool:
    bank_id = str(payload.get("bank_account_id") or "")
    cid = as_uuid(payload.get("candidate_id"))
    if not bank_id or cid is None:
        return False
    rows = (
        await db.execute(
            select(OnboardingTask)
            .where(
                OnboardingTask.candidate_id == cid,
                OnboardingTask.kind == "bank_details",
                OnboardingTask.status == "submitted",
            )
            .with_for_update()
        )
    ).scalars()
    for task in list(rows):
        if str((task.data or {}).get("bank_account_id")) != bank_id or not await _active(db, task):
            continue
        task.data = {**(task.data or {}), "change_requested": False}
        if payload.get("outcome") == "approved":
            await decide(ctx, db, task, "approved", "system")
        else:
            await decide(ctx, db, task, "returned", "system", comment="The bank details change was rejected")
        return True
    return False
