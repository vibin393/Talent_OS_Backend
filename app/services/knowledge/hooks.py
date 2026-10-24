# ruff: file-ignore[line-too-long, unused-function-argument]
"""Registrations of the ``knowledge`` domain (autodiscovered by ``core.hooks``), Epic 62.

* Grounded-conversation checker ``onboarding_qa`` (FR-274, coordinator decision 3).
* Default consent notice ``knowledge_assistant`` (English).
* Notification types ``onboarding_question_handoff`` (internal, coordinator) and ``onboarding_answer_ready`` (Worker
  portal, ``/portal/work/help/{thread_id}``).
* ``AgentRunFailed`` of an onboarding run -> "I'll pass this to your coordinator." + Task (``app.tasks.knowledge``).
* Settings hub entry "Knowledge sources", retention binding ``onboarding_qa_messages`` -> ``conversation_records``,
  workflow trigger ``knowledge.question_handed_off``.
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

NOTICE_TITLE = "Onboarding help assistant"
NOTICE_BODY = (
    "An **AI assistant** answers your onboarding questions on behalf of your coordinator.\n\n"
    "- It answers only from {tenant}'s published onboarding material and your own onboarding tasks, and shows the "
    "sources it used.\n"
    "- It does not give pay, tax, legal or immigration advice: those questions go to your coordinator.\n"
    "- The AI model provider is a sub-processor that may process your questions outside India.\n"
    "- Your questions and the answers are stored for 180 days, and your coordinator can see them.\n"
    '- You can ask for a person at any time ("Talk to my coordinator").\n'
    "- You can withdraw this consent at any time from your privacy settings; the assistant then stops and your "
    "coordinator answers instead."
)


def _uuid(v: Any) -> uuid.UUID | None:
    try:
        return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))
    except (TypeError, ValueError):
        return None


def _register_grounded() -> None:
    from app.services import agent_runtime_service
    from app.services.knowledge.qa import grounded_conversation

    agent_runtime_service.register_grounded_conversation_checker("onboarding_qa", grounded_conversation)


def _register_notice() -> None:
    from app.services.privacy.consent import register_default_notice

    register_default_notice(
        "knowledge_assistant", NOTICE_TITLE, NOTICE_BODY.replace("{tenant}", "your employer")
    )


async def _none(db: AsyncSession, payload: Any) -> set[Any]:  # NOSONAR
    return set()


async def _render_handoff(db: AsyncSession, payload: Any) -> Any:  # NOSONAR
    from app.services.notifications_service import Content

    tid = _uuid(payload.get("thread_id"))
    return Content(
        title="A worker asked an onboarding question",
        link_path=f"/onboarding/questions?thread={tid}" if tid else "/onboarding/questions",
    )


async def _render_answer(db: AsyncSession, payload: Any) -> Any:  # NOSONAR
    from app.services.notifications_service import Content

    return Content(title="Your onboarding question has an answer")


def _register_notifications() -> None:
    from app.services import notifications_service as ns

    defs: dict[str, Any] = {
        "onboarding_question_handoff": ns.NotificationType(
            "Onboarding question handed to you", "Onboarding", "both", False, _none, _render_handoff, internal_only=True
        ),
        "onboarding_answer_ready": ns.NotificationType(
            "Onboarding question answered", "Onboarding", "in_app", False, _none, _render_answer,
            external_default_channel="in_app", portal_path="/portal/work/help/{thread_id}", external=True,
        ),
    }  # fmt: skip
    for key, definition in defs.items():
        if key not in ns.NOTIFICATION_TYPES:
            ns.register_notification_type(key, definition)


def _subscribe() -> None:
    from app.core.events import subscribe
    from app.events.agents import AgentRunFailed

    def run_failed(e: AgentRunFailed) -> None:
        if e.agent_key == "onboarding":
            from app.tasks import knowledge as tasks
            from app.tasks.runtime import enqueue

            enqueue(tasks.run_failed, str(e.tenant_id), str(e.run_id), e.reason_code)

    def published(e: Any) -> None:
        from app.core.settings import get_settings

        if str(get_settings().embedding_backend) == "fake":
            return  # embedded inline when chunking
        from app.tasks import knowledge as tasks
        from app.tasks.runtime import enqueue

        enqueue(tasks.reindex, str(e.tenant_id), str(e.source_id))

    from app.events.agents import AgentRunFinished
    from app.events.knowledge import KnowledgeSourcePublished

    def run_finished(e: AgentRunFinished) -> None:
        # a run that ended without answering the latest question (no reply, stopped, cancelled) hands it over
        if e.agent_key == "onboarding":
            from app.tasks import knowledge as tasks
            from app.tasks.runtime import enqueue

            enqueue(tasks.run_failed, str(e.tenant_id), str(e.run_id), e.reason_code or e.status)

    run_finished.__name__ = "knowledge_on_AgentRunFinished"
    subscribe(AgentRunFinished, run_finished)

    run_failed.__name__ = "knowledge_on_AgentRunFailed"
    published.__name__ = "knowledge_on_KnowledgeSourcePublished"
    subscribe(AgentRunFailed, run_failed)
    subscribe(KnowledgeSourcePublished, published)


def _register_settings() -> None:
    from app.services.settings_service import register_settings_entry

    register_settings_entry(
        "workforce", "knowledge_sources", "Knowledge sources",
        "Onboarding articles, policies and FAQs the onboarding assistant may cite.",
        "/api/v1/knowledge/sources", "knowledge:manage", owner_epic="Epic 62",
    )  # fmt: skip


def _register_retention() -> None:
    from collections.abc import Sequence

    from sqlalchemy import delete, select

    from app.models.knowledge import OnboardingQaMessage
    from app.services.privacy.retention_classes import register_retention_binding, retention_bindings

    async def subject(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> uuid.UUID | None:
        return (
            await db.execute(select(OnboardingQaMessage.candidate_id).where(OnboardingQaMessage.id == row_id))
        ).scalar_one_or_none()

    async def anchor(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> Any:
        return (
            await db.execute(select(OnboardingQaMessage.created_at).where(OnboardingQaMessage.id == row_id))
        ).scalar_one_or_none()

    async def expire(db: AsyncSession, ids: Sequence[uuid.UUID], action: str = "delete") -> dict[str, int]:
        result = await db.execute(delete(OnboardingQaMessage).where(OnboardingQaMessage.id.in_(list(ids))))
        return {"deleted": int(getattr(result, "rowcount", 0) or 0)}

    if "onboarding_qa_messages" not in retention_bindings():
        register_retention_binding(
            "onboarding_qa_messages",
            "conversation_records",
            subject_resolver=subject,
            anchor_resolver=anchor,
            expire_fn=expire,
        )


def _register_workflow() -> None:
    from app.services.workflow.conditions import FieldDef
    from app.services.workflow.registry import TRIGGERS, Trigger, register_trigger

    key = "knowledge.question_handed_off"
    if key not in TRIGGERS:
        register_trigger(
            key,
            Trigger(
                key, "Onboarding question handed to a coordinator", "app.events.knowledge", "OnboardingQuestionHandedOff",
                "candidate", "candidate_id", {"reason": ("reason", FieldDef("str", "Reason"))},
            ),
        )  # fmt: skip


for _fn in (
    _register_grounded, _register_notice, _register_notifications, _subscribe, _register_settings, _register_retention,
    _register_workflow,
):  # fmt: skip
    try:
        _fn()
    except Exception:  # pragma: no cover - surfaced in the logs
        logger.exception("knowledge hooks: {} failed", _fn.__name__)
