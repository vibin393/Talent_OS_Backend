# ruff: file-ignore[line-too-long]
"""AI governance wiring (Epic 65), autodiscovered by ``core.hooks``.

* events (after commit -> ``ai_governance.*`` tasks): ``ProposalDecided`` -> SM-C14 review signal;
  ``AlternativeRequested`` -> stop agent screening; ``AgentRunStarted`` (screening / voice on an Application) -> cancel
  before step 1 when an alternative-process request exists;
* the notification type ``ai_eval_regression`` (internal; ``ai:agent_configure`` + ``ai:govern`` holders);
* header search groups ``agent_proposal`` / ``knowledge_source``; the Phase 4 settings entries no owner registered;
* the enablement checks; the retention binding of ``proposal_review_signals`` (class ``agent_logs``: rows deleted).
"""

import uuid
from collections.abc import Sequence
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import subscribe


def _subscribe() -> None:
    from app.events.agents import AgentRunStarted, ProposalDecided
    from app.services.fairness.events import AlternativeRequested

    def on_decided(e: ProposalDecided) -> None:
        from app.tasks import ai_governance as tasks
        from app.tasks.runtime import enqueue

        if e.outcome in ("approved", "edited_approved", "rejected") and e.decided_by is not None:
            enqueue(
                tasks.on_proposal_decided, str(e.tenant_id), str(e.proposal_id), str(e.decided_by), e.outcome
            )

    def on_alternative(e: AlternativeRequested) -> None:
        from app.tasks import ai_governance as tasks
        from app.tasks.runtime import enqueue

        enqueue(tasks.on_alternative_requested, str(e.tenant_id), str(e.request_id))

    def on_started(e: AgentRunStarted) -> None:
        from app.tasks import ai_governance as tasks
        from app.tasks.runtime import enqueue

        if e.agent_key in ("screening", "voice_screening") and e.subject_type == "application":
            enqueue(tasks.on_run_started, str(e.tenant_id), str(e.run_id))

    on_decided.__name__ = "ai_governance_on_proposal_decided"
    on_alternative.__name__ = "ai_governance_on_alternative_requested"
    on_started.__name__ = "ai_governance_on_agent_run_started"
    subscribe(ProposalDecided, on_decided)
    subscribe(AlternativeRequested, on_alternative)
    subscribe(AgentRunStarted, on_started)


def _register_notifications() -> None:
    from app.services import notifications_service as ns

    async def recipients(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # ruff: ignore[unused-function-argument]
        from app.services.agent_runtime.hooks import _holders

        return await _holders(db, "ai:agent_configure", "ai:govern")

    async def render(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]
        return ns.Content(
            title=f"AI agent {payload.get('agent_key')} fell below its evaluation threshold",
            body="Review the latest evaluation results before relying on this agent.",
            link_path="/admin/ai/evals",
        )

    if "ai_eval_regression" not in ns.NOTIFICATION_TYPES:
        ns.register_notification_type(
            "ai_eval_regression",
            ns.NotificationType(
                "AI evaluation regression", "AI agents", "both", False, recipients, render, internal_only=True
            ),
        )


def _register_search_and_settings() -> None:
    from app.services.ai_governance import search, settings_catalog_p4

    search.register()
    settings_catalog_p4.register()


def _register_checks() -> None:
    from app.services.ai_governance import checks

    checks.register()


def _register_retention() -> None:
    from sqlalchemy import delete, select

    from app.models.proposal_signals import ProposalReviewSignal
    from app.services.privacy.retention_classes import register_retention_binding, retention_bindings

    async def _subject(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> uuid.UUID | None:  # ruff: ignore[unused-function-argument]
        return None  # staff telemetry, no data subject

    async def _anchor(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> Any:
        return (
            await db.execute(select(ProposalReviewSignal.created_at).where(ProposalReviewSignal.id == row_id))
        ).scalar_one_or_none()

    async def _expire(
        db: AsyncSession, ids: Sequence[uuid.UUID], action: str = "anonymize"
    ) -> dict[str, int]:
        result = await db.execute(delete(ProposalReviewSignal).where(ProposalReviewSignal.id.in_(list(ids))))
        return {"deleted": int(getattr(result, "rowcount", 0) or 0)}

    if "proposal_review_signals" not in retention_bindings():
        register_retention_binding(
            "proposal_review_signals",
            "agent_logs",
            subject_resolver=_subject,
            anchor_resolver=_anchor,
            expire_fn=_expire,
        )


for _fn in (
    _subscribe,
    _register_notifications,
    _register_search_and_settings,
    _register_checks,
    _register_retention,
):
    try:
        _fn()
    except Exception:  # pragma: no cover - surfaced in the logs
        logger.exception("ai_governance hooks: {} failed", _fn.__name__)
        raise
