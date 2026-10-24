# ruff: file-ignore[line-too-long, unused-function-argument]
"""Returning-Worker reuse (Epic 23 A4, FR-124, UJ-14).

A ``reusable`` task is inserted as ``approved`` (``decided_by_kind='system'``, ``reused_from_task_id``) when the
candidate's latest approved task of the same kind and config identity is still valid:

* bank: an ``active`` bank account exists (placements);
* PAN / tax: ``pan_status = hr_checked`` (placements);
* documents: one of the previous task's documents is still current and unexpired;
* policy acknowledgements: the same published template version;
* otherwise: decided within ``validity_days`` (task, else ``reuse_validity_days``).

eSign and internal tasks are never reused (every Placement gets its own agreement and equipment).
"""

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.models.onboarding import OnboardingTask
from app.services.onboarding.common import now, today

NEVER_REUSED = frozenset({"esign", "internal"})


def identity(task: OnboardingTask) -> tuple[Any, ...]:  # NOSONAR
    cfg = task.config or {}
    if task.kind == "document_upload":
        return (task.kind, str(cfg.get("document_category") or ""), bool(cfg.get("aadhaar")))
    if task.kind == "credential":
        return (task.kind, str(cfg.get("credential_type_key") or ""))
    if task.kind == "policy_ack":
        return (task.kind, str(cfg.get("document_template_id") or ""))
    if task.kind == "client_form":
        return (task.kind, task.key)
    return (task.kind,)


async def _previous(db: AsyncSession, task: OnboardingTask) -> OnboardingTask | None:
    rows = (
        await db.execute(
            select(OnboardingTask)
            .where(
                OnboardingTask.candidate_id == task.candidate_id,
                OnboardingTask.kind == task.kind,
                OnboardingTask.status == "approved",
                OnboardingTask.case_id != task.case_id,
                OnboardingTask.decided_at.is_not(None),
            )
            .order_by(OnboardingTask.decided_at.desc())
            .limit(25)
        )
    ).scalars()
    want = identity(task)
    for row in rows:
        if identity(row) == want:
            return row
    return None


async def _still_valid(
    ctx: RequestContext, db: AsyncSession, task: OnboardingTask, prev: OnboardingTask, default_days: int
) -> bool:
    from app.services import placements_service

    if task.kind == "bank_details":
        ident = await placements_service.worker_payment_identity(ctx, db, task.candidate_id)
        return bool(ident.bank_active)
    if task.kind == "tax_details":
        ident = await placements_service.worker_payment_identity(ctx, db, task.candidate_id)
        return ident.pan_status == "hr_checked"
    if task.kind == "document_upload":
        return await _document_current(ctx, db, task.candidate_id, prev.document_ids or [])
    if task.kind == "policy_ack":
        from app.services import documents_service

        try:
            v = await documents_service.templates.published_version(
                db, uuid.UUID(str((task.config or {}).get("document_template_id")))
            )
        except Exception:
            return False
        return str((prev.data or {}).get("template_version_id")) == str(v.id)
    days = task.validity_days or prev.validity_days or default_days
    return prev.decided_at is not None and prev.decided_at >= now() - timedelta(days=days)


async def _document_current(
    ctx: RequestContext,
    db: AsyncSession,
    candidate_id: uuid.UUID,
    document_ids: list[uuid.UUID],
) -> bool:
    if not document_ids:
        return False
    from app.services import documents_service

    system = RequestContext(tenant_id=None, actor_kind="system")
    docs = await documents_service.list_documents(system, db, "candidate", candidate_id)
    wanted = {str(d) for d in document_ids}
    on = today()
    return any(
        str(d.id) in wanted and d.superseded_by_id is None and (d.expires_on is None or d.expires_on > on)
        for d in docs
    )


async def apply(ctx: RequestContext, db: AsyncSession, task: OnboardingTask, default_days: int) -> bool:
    """Pre-approve ``task`` from a still-valid earlier approval; returns whether it was reused."""
    if not task.reusable or task.kind in NEVER_REUSED or task.status != "pending":
        return False
    prev = await _previous(db, task)
    if prev is None or not await _still_valid(ctx, db, task, prev, default_days):
        return False
    task.status = "approved"
    task.reused_from_task_id = prev.id
    task.decided_by_kind = "system"
    task.decided_by_id = None
    task.decided_at = now()
    task.data = dict(prev.data or {})
    task.document_ids = list(prev.document_ids or [])
    task.credential_id = prev.credential_id
    await db.flush()
    audit.record(
        "onboarding.task_reused",
        "onboarding_task",
        task.id,
        session=db,
        reused_from=str(prev.id),
        kind=task.kind,
    )
    return True


# ---------------------------------------------------------------- "Provide credential": already-Valid credential
_VALID_CREDENTIAL = text(
    "SELECT id FROM credentials WHERE candidate_id = :cid AND type_key = :key "
    "AND verification_status = 'valid' AND superseded_at IS NULL AND erased_at IS NULL "
    "AND expiry_status <> 'expired' AND (expires_on IS NULL OR expires_on >= :on) "
    "ORDER BY verified_at DESC NULLS LAST, created_at DESC LIMIT 1"
)


async def valid_credential_id(db: AsyncSession, candidate_id: uuid.UUID, type_key: str) -> uuid.UUID | None:
    """Return the worker's current, unexpired, compliance-verified credential of ``type_key``, if any."""
    if not type_key:
        return None
    row = (await db.execute(_VALID_CREDENTIAL, {"cid": candidate_id, "key": type_key, "on": today()})).first()
    return row[0] if row else None


async def link_valid_credential(ctx: RequestContext, db: AsyncSession, task: OnboardingTask) -> bool:
    """Approve a new ``credential`` task at once (system) when the worker already holds a Valid credential.

    The credential (of the configured type) is linked to the task. Returns whether it was applied.
    """
    if task.kind != "credential" or task.status != "pending":
        return False
    cred = await valid_credential_id(db, task.candidate_id, str((task.config or {}).get("credential_type_key") or ""))
    if cred is None:
        return False
    task.status = "approved"
    task.credential_id = cred
    task.data = {"credential_id": str(cred)}
    task.decided_by_kind = "system"
    task.decided_by_id = None
    task.decided_at = now()
    await db.flush()
    audit.record("onboarding.task_credential_linked", "onboarding_task", task.id, session=db, credential_id=str(cred))
    return True
