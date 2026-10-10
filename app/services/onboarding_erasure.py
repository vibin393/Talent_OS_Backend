"""Onboarding erasure handler ``onboarding.cases`` (63), Epic 23 A10 (AD-34).

Claims ``onboarding_cases``, ``onboarding_tasks`` and ``onboarding_acknowledgements`` of the subject.

* Erase: ``draft`` / ``data`` / ``return_comment`` cleared (their audit values are crypto-shredded with the
  subject key, AD-5), ``waived_reason`` and acknowledgement IP / UA nulled; statuses, dates and keys are kept.
  Uploaded documents are erased by the documents handler, bank / PAN by the placements handler.
* Retain: rows listed in ``ctx.retained`` are left intact.
* Export: tasks with status and submitted values, plus acknowledgements.
* Verify: no submitted values remain for the subject.
"""

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.onboarding import OnboardingAcknowledgement, OnboardingCase, OnboardingTask
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler


class OnboardingHandler:
    name = "onboarding.cases"
    order = 63
    tables = ("onboarding_cases", "onboarding_tasks", "onboarding_acknowledgements")

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await subject_ids(db, ctx.candidate_id)
        kept_tasks = ctx.retained.get("onboarding_tasks", set())
        kept_acks = ctx.retained.get("onboarding_acknowledgements", set())
        counts = {"tasks": 0, "acknowledgements": 0, "retained": 0}
        tasks = (
            await db.execute(select(OnboardingTask).where(OnboardingTask.candidate_id.in_(ids)))
        ).scalars()
        for t in list(tasks):
            if t.id in kept_tasks:
                counts["retained"] += 1
                continue
            t.draft = None
            t.data = None
            t.return_comment = None
            t.waived_reason = None
            counts["tasks"] += 1
        acks = (
            await db.execute(
                select(OnboardingAcknowledgement).where(OnboardingAcknowledgement.candidate_id.in_(ids))
            )
        ).scalars()
        for a in list(acks):
            if a.id in kept_acks:
                counts["retained"] += 1
                continue
            a.ip = None
            a.user_agent = None
            counts["acknowledgements"] += 1
        for case in (
            await db.execute(select(OnboardingCase).where(OnboardingCase.candidate_id.in_(ids)))
        ).scalars():
            case.pending_requirements = {}
        await db.flush()
        return counts

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        tasks = list(
            (
                await db.execute(
                    select(OnboardingTask).where(OnboardingTask.candidate_id == ctx.candidate_id)
                )
            ).scalars()
        )
        acks = list(
            (
                await db.execute(
                    select(OnboardingAcknowledgement).where(
                        OnboardingAcknowledgement.candidate_id == ctx.candidate_id
                    )
                )
            ).scalars()
        )
        if not tasks and not acks:
            return None
        return {
            "tasks": [
                {
                    "label": t.label,
                    "kind": t.kind,
                    "status": t.status,
                    "submitted_at": t.submitted_at,
                    "submitted_values": t.data,
                    "decided_at": t.decided_at,
                }
                for t in tasks
                if t.assignee == "worker"
            ],
            "acknowledgements": [
                {"document_template_id": str(a.document_template_id), "acknowledged_at": a.acknowledged_at}
                for a in acks
            ],
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        kept = ctx.retained.get("onboarding_tasks", set())
        left = (
            await db.execute(
                select(OnboardingTask.id).where(
                    OnboardingTask.candidate_id.in_(ids),
                    or_(
                        OnboardingTask.data.is_not(None),
                        OnboardingTask.draft.is_not(None),
                        OnboardingTask.return_comment.is_not(None),
                    ),
                )
            )
        ).scalars()
        findings = [f"onboarding task {tid} still holds submitted values" for tid in left if tid not in kept]
        ack_left = (
            await db.execute(
                select(OnboardingAcknowledgement.id).where(
                    OnboardingAcknowledgement.candidate_id.in_(ids), OnboardingAcknowledgement.ip.is_not(None)
                )
            )
        ).first()
        if ack_left:
            findings.append("acknowledgement IP addresses remain")
        return findings


register_erasure_handler(OnboardingHandler())
