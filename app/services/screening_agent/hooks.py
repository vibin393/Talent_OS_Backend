# ruff: file-ignore[line-too-long, unused-function-argument]
"""Registrations of the ``screening_agent`` domain (autodiscovered by ``core.hooks``), Epic 57.

* Grounded-conversation checker ``screening`` (FR-274).
* Default consent notice ``ai_agent_communication`` (AI assistant for a named recruiter, sub-processor, a person any
  time, withdrawal).
* Notification types ``screening_completed`` / ``screening_handed_off`` (mandatory in-app) / ``screening_expired``
  (owner) and ``screening_chat_invited`` (Candidate Portal).
* Event subscriptions (after commit -> ``app.tasks.screening_agent``): inbound WhatsApp / email replies, consent
  withdrawal, workflow-started runs, failed runs, deferred run hand-overs.
* Settings hub entry, ★ transitions (``screening_conversation`` -> reviewed; ``screening_answer`` decisions), retention
  bindings (``conversation_records``), audit actions.
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

NOTICE_TITLE = "Talking with an AI assistant"
NOTICE_BODY = (
    "Some of our screening conversations are run by an **AI assistant** acting for a named recruiter.\n\n"
    "- The assistant asks the approved screening questions for the job you applied to and answers questions about the "
    "job only from the published job details.\n"
    "- Your answers are recorded and reviewed by the recruiter, who makes every decision. The assistant never decides "
    "on your application.\n"
    "- The AI model provider is a sub-processor that may process your messages outside India.\n"
    '- You can ask for a person at any time (reply "talk to a person" or use the button).\n'
    "- You can withdraw this consent at any time from the candidate portal or by replying STOP; the conversation then "
    "stops and a recruiter follows up."
)


def _uuid(v: Any) -> uuid.UUID | None:
    try:
        return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))
    except (TypeError, ValueError):
        return None


def _register_grounded() -> None:
    from app.services import agent_runtime_service
    from app.services.screening_agent.grounding import grounded_checker

    agent_runtime_service.register_grounded_conversation_checker("screening", grounded_checker)


def _register_notice() -> None:
    from app.services.privacy.consent import register_default_notice

    register_default_notice("ai_agent_communication", NOTICE_TITLE, NOTICE_BODY)


# ---------------------------------------------------------------- notifications
async def _owner(db: AsyncSession, payload: Any) -> set[uuid.UUID]:
    from app.models.screening_agent import ScreeningConversation

    cid = _uuid(payload.get("conversation_id"))
    conv = await db.get(ScreeningConversation, cid) if cid else None
    return {conv.owner_user_id} if conv is not None else set()


async def _candidate(db: AsyncSession, payload: Any) -> set[Any]:  # NOSONAR
    from app.services.notifications_service import Recipient

    cid = _uuid(payload.get("candidate_id"))
    return {Recipient("candidate", cid)} if cid else set()


def _renderer(kind: str) -> Any:
    async def render(db: AsyncSession, payload: Any) -> Any:
        from app.models.screening_agent import ScreeningConversation
        from app.services.notifications_service import Content

        cid = _uuid(payload.get("conversation_id"))
        conv = await db.get(ScreeningConversation, cid) if cid else None
        human = conv.human_id if conv is not None else "AI screening"
        app_id = conv.application_id if conv is not None else _uuid(payload.get("application_id"))
        titles = {
            "completed": f"{human}: AI screening answers are ready for your review",
            "handed_off": f"{human}: the AI assistant handed a screening conversation to you",
            "expired": f"{human}: an AI screening expired without answers",
            "invited": "A recruiter's AI assistant has a few questions for you",
        }
        if kind == "invited":
            return Content(
                title=titles[kind], link_path=f"/portal/screening/{cid}" if cid else "/portal/screening"
            )
        return Content(
            title=titles[kind][:300], entity_type="application", entity_id=app_id,
            link_path=f"/applications/{app_id}?panel=screening" if app_id else None,
        )  # fmt: skip

    return render


def _outcome_chooser(payload: Any) -> str | None:
    return {
        "completed": "screening_completed",
        "handed_off": "screening_handed_off",
        "expired": "screening_expired",
    }.get(str(payload.get("outcome")))


def _register_notifications() -> None:
    from app.events.conversations import ScreeningConversationCompleted
    from app.events.screening_agent import ScreeningChatInvited
    from app.services import notifications_service as ns

    defs: dict[str, Any] = {
        "screening_completed": ns.NotificationType("AI screening completed", "Screening", "both", False, _owner, _renderer("completed"), internal_only=True),
        "screening_handed_off": ns.NotificationType("AI screening handed off", "Screening", "both", True, _owner, _renderer("handed_off"), internal_only=True),
        "screening_expired": ns.NotificationType("AI screening expired", "Screening", "in_app", False, _owner, _renderer("expired"), internal_only=True),
        "screening_chat_invited": ns.NotificationType(
            "AI screening chat invitation", "Screening", "in_app", False, _candidate, _renderer("invited"),
            external_default_channel="in_app", portal_path="/portal/screening/{conversation_id}", external=True,
        ),
    }  # fmt: skip
    fresh = [k for k in defs if k not in ns.NOTIFICATION_TYPES]
    for key in fresh:
        ns.register_notification_type(key, defs[key])
    if "screening_completed" in fresh:
        ns.register_event_source(ScreeningConversationCompleted, _outcome_chooser)
        ns.register_event_source(ScreeningChatInvited, "screening_chat_invited")


# ---------------------------------------------------------------- events -> tasks
def _subscribe() -> None:
    from app.core.events import subscribe
    from app.events import subscribe_optional
    from app.events.agents import AgentRunFailed, AgentRunStarted
    from app.events.screening_agent import ScreeningRunNeeded

    def _enqueue(name: str, *args: Any) -> None:
        from app.tasks import screening_agent as tasks
        from app.tasks.runtime import enqueue

        enqueue(getattr(tasks, name), *args)

    def channel_inbound(e: Any) -> None:
        if e.matched and e.candidate_id is not None and e.channel == "whatsapp":
            _enqueue("channel_inbound", str(e.tenant_id), str(e.message_id))

    def email_reply(e: Any) -> None:
        _enqueue(
            "email_reply", str(e.tenant_id), str(e.inbound_email_id), str(e.email_message_id),
            str(e.candidate_id) if e.candidate_id else None,
        )  # fmt: skip

    def consent_withdrawn(e: Any) -> None:
        if e.purpose in ("ai_agent_communication", "ai_processing"):
            _enqueue("consent_withdrawn", str(e.tenant_id), str(e.candidate_id))

    def run_started(e: AgentRunStarted) -> None:
        if e.agent_key == "screening" and e.trigger_kind == "workflow":
            _enqueue("workflow_run", str(e.tenant_id), str(e.run_id))

    def run_failed(e: AgentRunFailed) -> None:
        if e.agent_key == "screening":
            _enqueue("run_failed", str(e.tenant_id), str(e.run_id), e.reason_code)

    def run_needed(e: ScreeningRunNeeded) -> None:
        _enqueue("run_needed", str(e.tenant_id), str(e.conversation_id), e.payload)

    for module, cls, fn in (
        ("app.events.comms", "ChannelInboundReceived", channel_inbound),
        ("app.events.comms", "EmailReplyReceived", email_reply),
        ("app.events.privacy", "ConsentWithdrawn", consent_withdrawn),
    ):
        fn.__name__ = f"screening_agent_on_{cls}"
        subscribe_optional(module, cls, fn)
    for event_cls, fn in (
        (AgentRunStarted, run_started),
        (AgentRunFailed, run_failed),
        (ScreeningRunNeeded, run_needed),
    ):
        fn.__name__ = f"screening_agent_on_{event_cls.__name__}"
        subscribe(event_cls, fn)  # type: ignore[arg-type]


# ---------------------------------------------------------------- settings / ★ / retention / audit
def _register_settings() -> None:
    from app.services.settings_service import register_settings_entry

    register_settings_entry(
        "recruiting", "screening_agent", "Screening Agent",
        "Expiry, reminders, questions per message, the low-confidence threshold, stop words and channels of AI screening.",
        "/api/v1/settings/agents/screening", "ai:agent_configure", owner_epic="Epic 57",
    )  # fmt: skip


def _register_human_only() -> None:
    from app.services.workflow.catalogue import register_human_only_transitions

    register_human_only_transitions("screening_conversation", ["reviewed"])
    register_human_only_transitions("screening_answer", ["accepted", "edited", "rejected"])


def _register_retention() -> None:
    from collections.abc import Sequence

    from sqlalchemy import delete, select, update

    from app.models.screening_agent import ScreeningAnswer, ScreeningTurn
    from app.services.privacy.retention_classes import register_retention_binding, retention_bindings

    def _subject(model: Any) -> Any:
        async def subject(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> uuid.UUID | None:
            return (
                await db.execute(select(model.candidate_id).where(model.id == row_id))
            ).scalar_one_or_none()

        return subject

    def _anchor(model: Any) -> Any:
        async def anchor(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> Any:
            return (await db.execute(select(model.created_at).where(model.id == row_id))).scalar_one_or_none()

        return anchor

    async def expire_turns(
        db: AsyncSession, ids: Sequence[uuid.UUID], action: str = "delete"
    ) -> dict[str, int]:
        result = await db.execute(delete(ScreeningTurn).where(ScreeningTurn.id.in_(list(ids))))
        return {"deleted": int(getattr(result, "rowcount", 0) or 0)}

    async def expire_answers(
        db: AsyncSession, ids: Sequence[uuid.UUID], action: str = "anonymize"
    ) -> dict[str, int]:
        result = await db.execute(
            update(ScreeningAnswer).where(ScreeningAnswer.id.in_(list(ids))).values(answer_text=None)
        )
        return {"redacted": int(getattr(result, "rowcount", 0) or 0)}

    bindings = retention_bindings()
    for table, model, fn in (
        ("screening_turns", ScreeningTurn, expire_turns),
        ("screening_answers", ScreeningAnswer, expire_answers),
    ):
        if table not in bindings:
            register_retention_binding(
                table,
                "conversation_records",
                subject_resolver=_subject(model),
                anchor_resolver=_anchor(model),
                expire_fn=fn,
            )


def _register_audit() -> None:
    from app.core import audit

    for key, text in (
        ("screening_conversation.started", "An AI screening conversation started"),
        ("screening_conversation.completed", "An AI screening conversation completed"),
        ("screening_conversation.handed_off", "An AI screening conversation was handed to the recruiter"),
        ("screening_conversation.withdrawn", "A candidate withdrew from an AI screening conversation"),
        ("screening_conversation.expired", "An AI screening conversation expired"),
        ("screening_conversation.consent_refused", "A candidate declined an AI screening conversation"),
        ("screening_conversation.paused", "An AI screening conversation was paused"),
        ("screening_conversation.resumed", "An AI screening conversation was resumed"),
        ("screening_conversation.taken_over", "A recruiter took over an AI screening conversation"),
        ("screening_conversation.cancelled", "An AI screening conversation was cancelled"),
        ("screening_questionnaire.version_approved", "A screening questionnaire version was approved"),
        (
            "screening_questionnaire.default_approved",
            "A default screening questionnaire was created and approved",
        ),
        ("screening_review.completed", "A recruiter completed the review of AI screening answers"),
    ):
        audit.register_audit_action(key, text)


for _fn in (
    _register_grounded, _register_notice, _register_notifications, _subscribe, _register_settings,
    _register_human_only, _register_retention, _register_audit,
):  # fmt: skip
    try:
        _fn()
    except Exception:  # pragma: no cover - surfaced in the logs
        logger.exception("screening_agent hooks: {} failed", _fn.__name__)
