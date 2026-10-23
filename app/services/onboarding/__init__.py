# ruff: file-ignore[line-too-long]
"""Facade of the ``onboarding`` domain (Epic 23, Digital Onboarding). Routers, tasks and hooks import from here.

Sibling domains (compliance, BGV) are reached by events only (AD-32); sensitive values go through placements
(AD-35) and signatures through documents (AD-28).
"""

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.services.onboarding import cases, portal, review, tasks, templates
from app.services.onboarding.common import as_uuid, get_settings_row, now, today

__all__ = ["cases", "handle_event", "portal", "review", "send_reminders", "tasks", "templates"]


async def handle_event(ctx: RequestContext, db: AsyncSession, name: str, payload: Mapping[str, Any]) -> Any:
    if name == "PlacementCreated":
        pid = as_uuid(payload.get("placement_id"))
        case = await cases.handle_placement_created(ctx, db, pid) if pid else None
        return str(case.id) if case else None
    if name == "PlacementDropped":
        pid = as_uuid(payload.get("placement_id"))
        return await cases.cancel_for_placement(ctx, db, pid, payload.get("reason")) if pid else False
    if name == "ComplianceRequirementsEvaluated":
        return await cases.handle_requirements(ctx, db, payload)
    if name == "CredentialRecorded":
        return await tasks.on_credential_recorded(ctx, db, payload)
    if name == "CredentialVerified":
        return await tasks.on_credential_verified(ctx, db, payload)
    if name == "SignatureRequestCompleted":
        return await tasks.on_signature_completed(ctx, db, payload)
    if name == "SignatureRequestStatusChanged":
        return await tasks.on_signature_status_changed(ctx, db, payload)
    if name == "TaskCompleted":
        return await tasks.on_p1_task_completed(ctx, db, payload)
    if name == "BankDetailsChangeDecided":
        return await tasks.on_bank_change_decided(ctx, db, payload)
    return None


async def send_reminders(ctx: RequestContext, db: AsyncSession) -> int:
    """Email Workers about overdue Worker tasks every ``worker_reminder_every_days`` (dedup by
    ``last_reminded_at``); emits ``OnboardingTaskOverdue`` per overdue task once per reminder."""
    from app.core.events import emit
    from app.events.onboarding import OnboardingTaskOverdue
    from app.models.onboarding import OnboardingCase, OnboardingTask
    from app.services.onboarding import notify

    settings = await get_settings_row(db)
    every = timedelta(days=max(1, settings.worker_reminder_every_days))
    cutoff = now() - every
    rows = (
        await db.execute(
            select(OnboardingCase)
            .where(
                OnboardingCase.status.in_(("not_started", "in_progress")),
                OnboardingCase.initialized_at.is_not(None),
                (OnboardingCase.last_reminded_at.is_(None)) | (OnboardingCase.last_reminded_at < cutoff),
            )
            .with_for_update(skip_locked=True)
        )
    ).scalars()
    sent = 0
    on = today()
    for case in list(rows):
        overdue = list(
            (
                await db.execute(
                    select(OnboardingTask).where(
                        OnboardingTask.case_id == case.id,
                        OnboardingTask.assignee == "worker",
                        OnboardingTask.status.in_(("pending", "returned")),
                        OnboardingTask.due_on < on,
                    )
                )
            ).scalars()
        )
        if not overdue:
            continue
        case.last_reminded_at = now()
        for t in overdue:
            t.last_reminded_at = now()
            emit(db, OnboardingTaskOverdue(tenant_id=case.tenant_id, task_id=t.id, case_id=case.id))
        await db.flush()
        await notify.to_worker(
            db,
            notify.TASK_OVERDUE,
            case.candidate_id,
            f"{len(overdue)} onboarding task(s) are overdue",
            "Sign in to the candidate portal and open My Work > Onboarding.",
        )
        sent += 1
    del ctx
    return sent
