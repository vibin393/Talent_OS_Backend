# ruff: file-ignore[line-too-long]
"""Screening conversations (Epic 57 stories 57.2-57.4, 57.6; spec A3).

Lifecycle: ``invited`` -> (disclosure + consent) ``consented`` -> ``in_progress`` -> ``completed`` (then ``reviewed``)
or ``handed_off`` / ``expired`` / ``withdrawn`` / ``consent_refused`` / ``cancelled`` / ``taken_over``; ``paused`` is
reversible. One open conversation per Application (``uq_screening_conv_open``).

A conversation is a chain of short agent runs (coordinator C-12): ``start`` launches the first run; a candidate reply
resumes the waiting run (``resume("reply", conversation_id)``) or starts a continuation run (``parent_run_id``,
dedup ``screening:conv:<id>``). Conversation state lives here, never in model history. ``agent_runs`` is joined by
``current_run_id`` / ``conversation_id`` + ``agent_key`` (no FK, C-1).
"""

import json
import uuid
from datetime import timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import actor_from_ctx
from app.core.agentic import Kickoff
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.events import emit
from app.core.sequences import next_human_id
from app.events.conversations import ScreeningConversationCompleted
from app.models.pipeline import Application, PipelineStage
from app.models.screening_agent import OPEN_STATUSES, ScreeningAnswer, ScreeningConversation
from app.schemas.screening_agent import ChannelOption, ConversationDetailOut, ConversationOut, StartOptionsOut
from app.schemas.tasks import TaskCreate
from app.services import agent_runtime_service as runtime
from app.services import entity_refs
from app.services.screening_agent import channels, turns
from app.services.screening_agent.common import (
    AGENT_KEY,
    FEATURE,
    conflict,
    conversation,
    is_open,
    now,
    required_ids,
    settings_row,
    tenant_of,
    threshold,
    version,
    wa_window_open,
)
from app.services.screening_agent.questionnaires import resolve_for_application

ACTIVE_RUN = ("queued", "running", "waiting")


# ================================================================ reads
async def _answered_ids(db: AsyncSession, conv: ScreeningConversation) -> list[uuid.UUID]:
    rows = (
        await db.execute(
            select(ScreeningAnswer.question_id, ScreeningAnswer.confidence, ScreeningAnswer.normalized).where(
                ScreeningAnswer.conversation_id == conv.id,
                ScreeningAnswer.superseded_at.is_(None),
                ScreeningAnswer.status != "rejected",
            )
        )
    ).all()
    limit = threshold(await settings_row(db))
    return [r[0] for r in rows if r[2] is not None and r[1] is not None and r[1] >= limit]


async def conversation_out(db: AsyncSession, conv: ScreeningConversation) -> ConversationOut:
    ver = await version(db, conv.questionnaire_version_id)
    required = required_ids(list(ver.questions or []))
    answered = set(await _answered_ids(db, conv))
    from app.models.pipeline import ScreeningQuestionnaire

    qn = await db.get(ScreeningQuestionnaire, ver.questionnaire_id)
    return ConversationOut(
        id=conv.id, human_id=conv.human_id, application_id=conv.application_id, candidate_id=conv.candidate_id,
        demand_id=conv.demand_id, screening_id=conv.screening_id, questionnaire_version_id=conv.questionnaire_version_id,
        questionnaire_label=f"{qn.name if qn is not None else 'Questionnaire'} v{ver.version}",
        channel=conv.channel, status=conv.status, current_run_id=conv.current_run_id,
        owner_user_id=conv.owner_user_id, owner_name=await channels.owner_name(db, conv), invited_at=conv.invited_at,
        consented_at=conv.consented_at, completed_at=conv.completed_at, expires_at=conv.expires_at,
        last_inbound_at=conv.last_inbound_at, last_outbound_at=conv.last_outbound_at,
        wa_window_open=wa_window_open(conv), reminders_sent=conv.reminders_sent, handoff_reason=conv.handoff_reason,
        taken_over_by=conv.taken_over_by, required_count=len(required),
        answered_count=len([q for q in required if q in answered]), summary=conv.summary, gaps=list(conv.gaps or []),
        flags=list(conv.flags or []), created_at=conv.created_at,
    )  # fmt: skip


async def _readable_app(ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID) -> Application:
    from app.services.pipeline.common import READ, application_for

    return await application_for(ctx, db, application_id, READ)


async def list_for_application(
    ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID
) -> list[ConversationOut]:
    await _readable_app(ctx, db, application_id)
    rows = (
        await db.execute(
            select(ScreeningConversation)
            .where(ScreeningConversation.application_id == application_id)
            .order_by(ScreeningConversation.created_at.desc())
        )
    ).scalars()
    return [await conversation_out(db, r) for r in rows]


async def readable(
    ctx: RequestContext, db: AsyncSession, conversation_id: uuid.UUID, *, for_update: bool = False
) -> ScreeningConversation:
    conv = await conversation(db, conversation_id, for_update=for_update)
    try:
        await _readable_app(ctx, db, conv.application_id)
    except NotFound as exc:
        raise NotFound("Screening conversation not found") from exc
    return conv


async def get(
    ctx: RequestContext, db: AsyncSession, conversation_id: uuid.UUID, after_seq: int = 0
) -> ConversationDetailOut:
    conv = await readable(ctx, db, conversation_id)
    base = await conversation_out(db, conv)
    rows = await turns.list_turns(db, conv.id, after_seq)
    return ConversationDetailOut(**base.model_dump(), turns=[turns.turn_out(t) for t in rows])


# ================================================================ start
async def _closed(db: AsyncSession, app: Application) -> bool:
    from app.services.pipeline.stages import TERMINAL

    if app.archived_at is not None:
        return True
    stage = await db.get(PipelineStage, app.stage_id)
    return stage is not None and stage.key in TERMINAL


async def alternative_process_requested(
    db: AsyncSession, candidate_id: uuid.UUID, demand_id: uuid.UUID
) -> bool:
    """LL144 (P3 FR-249): a Candidate's alternative-process request for this Demand blocks agent screening."""
    from app.services.matching.ports import suppressed_pairs

    hidden = await suppressed_pairs(db, [(candidate_id, demand_id)])
    return hidden.get((candidate_id, demand_id)) == "alternative_process"


async def _portal_active(candidate_id: uuid.UUID) -> bool:
    from app.core.database import platform_session
    from app.models.identity import Session

    async with platform_session() as pdb:
        found = (
            await pdb.execute(
                select(Session.id)
                .where(Session.principal_type == "candidate", Session.principal_id == candidate_id)
                .limit(1)
            )
        ).first()
    return found is not None


async def channel_options(db: AsyncSession, app: Application) -> list[ChannelOption]:
    from app.services.comms import channel_settings
    from app.services.messaging import opt_outs
    from app.services.messaging.sending import has_whatsapp_consent, resolve_phone

    who = await entity_refs.addressee(db, "candidate", app.candidate_id)
    has_email = who is not None and bool(who.address)
    portal = await _portal_active(app.candidate_id)
    settings = await settings_row(db)
    phone = await resolve_phone(db, "candidate", app.candidate_id)
    wa_reason: str | None = None
    if not phone:
        wa_reason = "No mobile number"
    elif await opt_outs.is_opted_out(db, "whatsapp", phone):
        wa_reason = "The candidate opted out of WhatsApp"
    elif not await has_whatsapp_consent(db, "candidate", app.candidate_id):
        wa_reason = "No WhatsApp opt-in"
    elif settings.whatsapp_invite_template_id is None:
        wa_reason = "No WhatsApp invitation template is set"
    else:
        row = await channel_settings.get_row(db)
        if not row.whatsapp_enabled:
            wa_reason = "WhatsApp is turned off"
    return [
        ChannelOption(
            channel="portal_chat",
            available=portal,
            reason=None if portal else "The candidate has not used the candidate portal",
        ),
        ChannelOption(
            channel="link_chat", available=has_email, reason=None if has_email else "No email address"
        ),
        ChannelOption(channel="email", available=has_email, reason=None if has_email else "No email address"),
        ChannelOption(channel="whatsapp", available=wa_reason is None, reason=wa_reason),
    ]


def _best(options: list[ChannelOption]) -> str | None:
    by = {o.channel: o for o in options}
    for key in ("portal_chat", "whatsapp", "link_chat"):
        if by[key].available:
            return key
    return None


async def _feature_on(db: AsyncSession) -> bool:
    from app.services import ai

    features = await ai.features_map(db)
    return bool(features.get("agentic")) and bool(features.get(FEATURE))


async def _block_reason(db: AsyncSession, app: Application) -> str | None:
    if not await _feature_on(db):
        return "screening_agent_off"
    if await _closed(db, app):
        return "application_closed"
    open_conv = (
        await db.execute(
            select(ScreeningConversation.id).where(
                ScreeningConversation.application_id == app.id,
                ScreeningConversation.status.in_(OPEN_STATUSES),
            )
        )
    ).first()
    if open_conv is not None:
        return "screening_conversation_open"
    if await alternative_process_requested(db, app.candidate_id, app.demand_id):
        return "alternative_process_requested"
    settings = await settings_row(db)
    if not settings.allow_campus_candidates:
        from app.models.candidates import Candidate

        cand = await db.get(Candidate, app.candidate_id)
        if cand is not None and cand.source == "campus":
            return "campus_candidate"
    return None


BLOCK_MESSAGES = {
    "screening_agent_off": "The Screening Agent is off",
    "application_closed": "The application is closed",
    "screening_conversation_open": "An AI screening is already open",
    "alternative_process_requested": "The candidate asked for a non-AI process",
    "campus_candidate": "AI screening is off for campus candidates",
    "no_channel": "No channel to reach this candidate",
}


async def start_options(ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID) -> StartOptionsOut:
    app = await _readable_app(ctx, db, application_id)
    options = await channel_options(db, app)
    ver = await resolve_for_application(ctx, db, app.id, create=False)
    name = None
    if ver is not None:
        from app.models.pipeline import ScreeningQuestionnaire

        qn = await db.get(ScreeningQuestionnaire, ver.questionnaire_id)
        name = qn.name if qn is not None else None
    best = _best(options)
    blocked = await _block_reason(db, app) or (None if best else "no_channel")
    return StartOptionsOut(
        channels=options, best_channel=best, questionnaire_id=ver.questionnaire_id if ver else None,
        questionnaire_name=name or ("Default questions + the demand's skills" if ver is None else None),
        version_id=ver.id if ver else None, version=ver.version if ver else None, blocked_reason=blocked,
    )  # fmt: skip


async def start(
    ctx: RequestContext,
    db: AsyncSession,
    application_id: uuid.UUID,
    *,
    channel: str = "auto",
    trigger_kind: str = "manual",
    run_id: uuid.UUID | None = None,
) -> ScreeningConversation:
    """Open a conversation and (unless ``run_id`` -- the workflow path) start its first run. 409 refusals:
    ``screening_conversation_open``, ``application_closed``, ``alternative_process_requested``, ``screening_agent_off``,
    ``no_channel``, ``campus_candidate``, ``screening_agent_unavailable``."""
    if ctx.principal is not None and getattr(ctx.principal, "type", None) == "user":
        app = await _readable_app(ctx, db, application_id)
    else:
        app = await db.get(Application, application_id)
        if app is None:
            raise NotFound("Application not found")
    reason = await _block_reason(db, app)
    if reason is not None:
        raise conflict(reason, BLOCK_MESSAGES[reason])
    options = await channel_options(db, app)
    settings = await settings_row(db)
    wanted = settings.default_channel if channel == "auto" else channel
    chosen = _best(options) if wanted == "auto" else wanted
    if chosen is None or not next(o for o in options if o.channel == chosen).available:
        raise conflict("no_channel", BLOCK_MESSAGES["no_channel"], channel=chosen)
    ver = await resolve_for_application(ctx, db, app.id)
    assert ver is not None
    starter = actor_from_ctx(ctx)
    conv = ScreeningConversation(
        id=uuid.uuid4(), tenant_id=tenant_of(db), human_id=await next_human_id(db, "screening_conversation", "SCR"),
        application_id=app.id, candidate_id=app.candidate_id, demand_id=app.demand_id, questionnaire_version_id=ver.id,
        channel=chosen, status="invited", owner_user_id=app.owner_id, started_by_kind=starter.kind,
        started_by_id=starter.id, expires_at=now() + timedelta(days=settings.expiry_days), current_run_id=run_id,
        flags=[], gaps=[], reminders_sent=0,
    )  # fmt: skip
    try:
        async with db.begin_nested():
            db.add(conv)
            await db.flush()
    except IntegrityError as exc:
        raise conflict("screening_conversation_open", BLOCK_MESSAGES["screening_conversation_open"]) from exc
    await turns.ensure_transcript(db, conv)
    audit.record(
        "screening_conversation.started", "screening_conversation", conv.id, session=db, channel=chosen,
        trigger_kind=trigger_kind,
    )  # fmt: skip
    if run_id is None:
        ref = await start_run(
            ctx,
            db,
            conv,
            trigger_kind=trigger_kind,
            trigger_key="manual" if trigger_kind == "manual" else "stage_entry",
            mode="start",
        )
        if ref.run_id is None:
            raise conflict(
                "screening_agent_unavailable",
                "The Screening Agent cannot run now",
                reason=ref.reason or "not_started",
            )
    return conv


# ================================================================ runs
async def kickoff(
    db: AsyncSession,
    conv: ScreeningConversation,
    mode: str,
    *,
    event: str | None = None,
    reply: str | None = None,
) -> Kickoff:
    ver = await version(db, conv.questionnaire_version_id)
    settings = await settings_row(db)
    inputs = {
        "conversation_id": str(conv.id),
        "application_id": str(conv.application_id),
        "channel": conv.channel,
        "conversation_status": conv.status,
        "questionnaire_version_id": str(ver.id),
        "required_question_ids": json.dumps([str(q) for q in required_ids(list(ver.questions or []))]),
        "answered_question_ids": json.dumps([str(q) for q in await _answered_ids(db, conv)]),
        "recruiter_name": await channels.owner_name(db, conv),
        "wa_window_open": "true" if wa_window_open(conv) else "false",
        "questions_per_turn": str(settings.questions_per_turn),
        "mode": mode,
    }
    if event:
        inputs["event"] = event
    untrusted = {"candidate_reply": reply} if reply else {}
    return Kickoff(inputs=inputs, untrusted=untrusted, candidate_ids=(conv.candidate_id,))


async def start_run(
    ctx: RequestContext,
    db: AsyncSession,
    conv: ScreeningConversation,
    *,
    trigger_kind: str,
    trigger_key: str,
    mode: str,
    event: str | None = None,
    reply: str | None = None,
    dedup_key: str | None = None,
    track: bool = True,
) -> Any:
    kick = await kickoff(db, conv, mode, event=event, reply=reply)
    ref = await runtime.start_run(
        ctx, db, agent_key=AGENT_KEY, trigger_kind=trigger_kind, trigger_key=trigger_key,
        subject=("application", conv.application_id), kickoff=kick, owner_user_id=conv.owner_user_id,
        dedup_key=dedup_key or f"screening:conv:{conv.id}", parent_run_id=conv.current_run_id,
        conversation_id=conv.id, origin="conversation",
    )  # fmt: skip
    if track and ref.run_id is not None and ref.reason not in ("attached", "deduplicated"):
        conv.current_run_id = ref.run_id
        await db.flush()
    return ref


async def _run_status(db: AsyncSession, run_id: uuid.UUID | None) -> str | None:
    if run_id is None:
        return None
    from app.models.agent_runtime import AgentRun

    row = await db.get(AgentRun, run_id)
    return row.status if row is not None else None


async def wake(
    ctx: RequestContext,
    db: AsyncSession,
    conv: ScreeningConversation,
    *,
    payload: dict[str, Any] | None = None,
    reply: str | None = None,
    event: str | None = None,
) -> str:
    """Hand a trusted event or a candidate reply to the agent: resume the waiting run, else start a continuation run
    (C-12). Returns ``resumed`` | ``continued`` | ``queued_retry`` | ``not_started``."""
    refs = await runtime.resume(
        ctx, db, condition="reply", ref=conv.id, payload=payload, untrusted_text=reply
    )
    if refs:
        return "resumed"
    status = await _run_status(db, conv.current_run_id)
    if status in ("queued", "running"):
        from app.events.screening_agent import ScreeningRunNeeded

        emit(db, ScreeningRunNeeded(
            tenant_id=conv.tenant_id, conversation_id=conv.id, trigger_kind="inbound", trigger_key="reply",
            mode="retry", payload=json.dumps({"payload": payload, "has_reply": reply is not None, "event": event}),
        ))  # fmt: skip
        return "queued_retry"
    if reply is not None:
        from app.services.agent_runtime.sanitize import prescreen

        if await prescreen(ctx, db, reply) == "suspicious":
            await flag(
                db,
                conv,
                "suspicious_input",
                "A candidate reply looked like an instruction to the AI. The assistant waits for you.",
            )
            await _task(
                db,
                conv,
                "Check a flagged screening reply",
                "A candidate reply was flagged as a possible instruction to the AI assistant. Review the conversation and resume or take over.",
            )
            return "not_started"
    ref = await start_run(
        ctx, db, conv, trigger_kind="inbound", trigger_key="reply", mode="continue", event=event, reply=reply
    )
    return "continued" if ref.run_id is not None else "not_started"


# ================================================================ state changes
async def flag(db: AsyncSession, conv: ScreeningConversation, code: str, message: str) -> None:
    flags = list(conv.flags or [])
    flags.append({"code": code, "message": message[:300], "at": now().isoformat()})
    conv.flags = flags[-20:]
    await db.flush()


async def _task(db: AsyncSession, conv: ScreeningConversation, title: str, notes: str) -> uuid.UUID | None:
    from app.services import tasks_service

    data = TaskCreate.model_construct(
        title=title[:200], notes=f"{notes}\n\nScreening conversation {conv.human_id}."[:4000], owner_id=conv.owner_user_id,
        entity_type="application", entity_id=conv.application_id, due_at=None, due_all_day=False, priority="normal",
        reminder_at=None,
    )  # fmt: skip
    try:
        async with db.begin_nested():
            out = await tasks_service.create_system_task(
                RequestContext(tenant_id=conv.tenant_id), db, data, source="agent"
            )
        return out.id
    except Exception:
        logger.warning("Screening {}: could not create the task {!r}", conv.id, title)
        return None


async def _cancel_run(
    ctx: RequestContext, db: AsyncSession, conv: ScreeningConversation, *, reason_code: str = "manual"
) -> None:
    if conv.current_run_id is None:
        return
    status = await _run_status(db, conv.current_run_id)
    if status in ACTIVE_RUN:
        await runtime.cancel_run(
            RequestContext(tenant_id=conv.tenant_id, actor_kind="system", request_id=ctx.request_id), db,
            conv.current_run_id, reason=f"screening conversation {conv.status}", reason_code=reason_code,
        )  # fmt: skip


def _completed_event(conv: ScreeningConversation, outcome: str) -> ScreeningConversationCompleted:
    return ScreeningConversationCompleted(
        tenant_id=conv.tenant_id, conversation_id=conv.id, application_id=conv.application_id,
        candidate_id=conv.candidate_id, outcome=outcome, transcript_id=conv.transcript_id, run_id=conv.current_run_id,
    )  # fmt: skip


async def handoff(
    ctx: RequestContext,
    db: AsyncSession,
    conv: ScreeningConversation,
    reason: str,
    note: str | None = None,
    *,
    cancel: bool = True,
    run_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """End the AI part: templated acknowledgement, ``handed_off``, a recruiter Task, the completion event."""
    from app.services.screening_agent.common import HANDOFF_ACK

    if not is_open(conv):
        return None
    turn = await turns.add_turn(
        db,
        conv,
        direction="outbound",
        author="agent",
        kind="handoff",
        body=HANDOFF_ACK,
        template_key="handoff_ack",
        run_id=run_id,
    )
    await channels.deliver(ctx, db, conv, turn)
    conv.status = "handed_off"
    conv.handoff_reason = reason
    conv.completed_at = now()
    task_id = await _task(
        db, conv, "Follow up an AI screening hand-off",
        f"The Screening Agent handed the conversation to you (reason: {reason.replace('_', ' ')}).{(' Note: ' + note) if note else ''}",
    )  # fmt: skip
    if cancel:
        await _cancel_run(ctx, db, conv)
    emit(db, _completed_event(conv, "handed_off"))
    audit.record(
        "screening_conversation.handed_off", "screening_conversation", conv.id, session=db, reason=reason
    )
    await db.flush()
    return task_id


async def end_for_alternative_process(
    ctx: RequestContext, db: AsyncSession, conv: ScreeningConversation
) -> None:
    if not is_open(conv):
        return
    await _cancel_run(ctx, db, conv, reason_code="alternative_process")
    await handoff(
        ctx,
        db,
        conv,
        "alternative_process",
        "The candidate asked for an alternative selection process.",
        cancel=False,
    )


async def withdraw(ctx: RequestContext, db: AsyncSession, conv: ScreeningConversation) -> None:
    """``ai_agent_communication`` (or AI-processing) consent withdrawn: stop before the next turn."""
    if not is_open(conv):
        return
    conv.status = "withdrawn"
    conv.completed_at = now()
    await _cancel_run(ctx, db, conv)
    await _task(
        db,
        conv,
        "Candidate withdrew AI screening consent",
        "The candidate withdrew consent to talk with the AI assistant. Screen manually if needed.",
    )
    emit(db, _completed_event(conv, "withdrawn"))
    audit.record("screening_conversation.withdrawn", "screening_conversation", conv.id, session=db)
    await db.flush()


async def expire(ctx: RequestContext, db: AsyncSession, conv: ScreeningConversation) -> None:
    if not is_open(conv):
        return
    conv.status = "expired"
    conv.completed_at = now()
    await _cancel_run(ctx, db, conv)
    await _task(
        db,
        conv,
        "AI screening expired",
        "The candidate did not finish the AI screening in time. Follow up or screen manually.",
    )
    emit(db, _completed_event(conv, "expired"))
    audit.record("screening_conversation.expired", "screening_conversation", conv.id, session=db)
    await db.flush()


async def refuse_consent(ctx: RequestContext, db: AsyncSession, conv: ScreeningConversation) -> None:
    conv.status = "consent_refused"
    conv.completed_at = now()
    await turns.add_turn(
        db,
        conv,
        direction="outbound",
        author="system",
        kind="closing",
        body="No problem. A recruiter will follow up with you directly.",
        template_key="consent_declined",
    )
    await _cancel_run(ctx, db, conv)
    await _task(
        db, conv, "Candidate declined AI screening",
        "The candidate declined to chat with the AI assistant. Start a manual screening from the application.",
    )  # fmt: skip
    audit.record("screening_conversation.consent_refused", "screening_conversation", conv.id, session=db)
    await db.flush()


async def grant_consent(
    ctx: RequestContext,
    db: AsyncSession,
    conv: ScreeningConversation,
    *,
    evidence: dict[str, Any],
    notice_id: uuid.UUID | None = None,
) -> None:
    """Record ``ai_agent_communication`` consent (source ``conversation``) -- and AI-processing consent where the
    tenant basis requires it -- then wake the agent with a trusted ``consent_granted`` event."""
    from app.services import privacy
    from app.services.privacy.consent import ai_consent_checker

    if conv.status != "invited":
        raise conflict("consent_not_pending", "This chat is not waiting for consent")
    cctx = RequestContext(tenant_id=conv.tenant_id, actor_kind="system")
    row = await privacy.grant_consent(
        cctx, db, conv.candidate_id, "ai_agent_communication", source="conversation", notice_id=notice_id,
        evidence={**evidence, "conversation_id": str(conv.id)},
    )  # fmt: skip
    decision = await ai_consent_checker(cctx, db, FEATURE, ("candidate", conv.candidate_id))
    if not decision.allowed:
        await privacy.grant_consent(
            cctx, db, conv.candidate_id, "ai_processing", source="conversation",
            evidence={**evidence, "conversation_id": str(conv.id)},
        )  # fmt: skip
    conv.consent_id = row.id
    conv.status = "consented"
    conv.consented_at = now()
    await db.flush()
    await wake(ctx, db, conv, payload={"event": "consent_granted"}, event="consent_granted")


async def pause(
    ctx: RequestContext, db: AsyncSession, conversation_id: uuid.UUID, *, reason: str | None = None
) -> ScreeningConversation:
    """Pause the agent (the run is cancelled; inbound replies are stored but unanswered). Exported for Epic 65 (C-16)."""
    conv = await conversation(db, conversation_id, for_update=True)
    if conv.status not in ("invited", "consented", "in_progress"):
        raise conflict(
            "conversation_not_active", "Only an active conversation can be paused", status=conv.status
        )
    conv.status = "paused"
    conv.paused_by = getattr(ctx.principal, "id", None)
    await _cancel_run(ctx, db, conv)
    audit.record(
        "screening_conversation.paused",
        "screening_conversation",
        conv.id,
        session=db,
        reason=(reason or "")[:500],
    )
    await db.flush()
    return conv


async def resume(ctx: RequestContext, db: AsyncSession, conversation_id: uuid.UUID) -> ScreeningConversation:
    conv = await conversation(db, conversation_id, for_update=True)
    if conv.status != "paused":
        raise conflict("conversation_not_paused", "The conversation is not paused", status=conv.status)
    conv.status = "in_progress" if conv.consented_at is not None else "invited"
    conv.paused_by = None
    await db.flush()
    ref = await start_run(
        ctx,
        db,
        conv,
        trigger_kind="manual",
        trigger_key="manual",
        mode="continue",
        event="resumed_by_recruiter",
    )
    if ref.run_id is None:
        raise conflict(
            "screening_agent_unavailable",
            "The Screening Agent cannot run now",
            reason=ref.reason or "not_started",
        )
    audit.record("screening_conversation.resumed", "screening_conversation", conv.id, session=db)
    return conv


async def take_over(
    ctx: RequestContext, db: AsyncSession, conversation_id: uuid.UUID
) -> ScreeningConversation:
    conv = await conversation(db, conversation_id, for_update=True)
    if conv.status not in OPEN_STATUSES:
        raise conflict(
            "conversation_not_active", "Only an open conversation can be taken over", status=conv.status
        )
    await _cancel_run(ctx, db, conv)
    conv.status = "taken_over"
    conv.taken_over_by = getattr(ctx.principal, "id", None)
    audit.record("screening_conversation.taken_over", "screening_conversation", conv.id, session=db)
    await db.flush()
    return conv


async def cancel(ctx: RequestContext, db: AsyncSession, conversation_id: uuid.UUID) -> ScreeningConversation:
    conv = await conversation(db, conversation_id, for_update=True)
    if conv.status not in (*OPEN_STATUSES, "taken_over"):
        raise conflict("conversation_not_active", "The conversation has already ended", status=conv.status)
    await _cancel_run(ctx, db, conv)
    conv.status = "cancelled"
    conv.completed_at = now()
    audit.record("screening_conversation.cancelled", "screening_conversation", conv.id, session=db)
    await db.flush()
    return conv


async def post_recruiter_message(
    ctx: RequestContext, db: AsyncSession, conversation_id: uuid.UUID, body: str
) -> Any:
    """After a take-over the recruiter writes as themself (``origin='user'``)."""
    from app.services.agent_runtime.sanitize import outbound

    conv = await conversation(db, conversation_id, for_update=True)
    if conv.status != "taken_over":
        raise conflict("not_taken_over", "Take over the conversation before writing to the candidate")
    if conv.channel == "whatsapp" and not wa_window_open(conv):
        raise conflict("window_closed", "WhatsApp only allows template messages now")
    user_id = getattr(ctx.principal, "id", None)
    turn = await turns.add_turn(
        db,
        conv,
        direction="outbound",
        author="recruiter",
        kind="recruiter_message",
        body=outbound(body).text,
        author_user_id=user_id,
    )
    await channels.deliver(ctx, db, conv, turn, origin="user", sender_user_id=user_id)
    return turn


async def complete(
    ctx: RequestContext,
    db: AsyncSession,
    conv: ScreeningConversation,
    *,
    summary: str,
    gaps: list[str],
    suggested_next: str | None,
    run_id: uuid.UUID | None,
) -> None:
    """``save_summary(final=true)``: ``completed``; the P1 ``screenings`` row (kind ``ai``) receives the answers and the
    summary through the additive P1 ``record_agent_result`` so the unchanged P1 review / outcome path applies."""
    from app.schemas.pipeline import Gap, ScreeningSummary, StructuredAnswer
    from app.services.pipeline import screening as p1

    ver = await version(db, conv.questionnaire_version_id)
    by_id = {q["id"]: q for q in ver.questions}
    rows = (
        await db.execute(
            select(ScreeningAnswer).where(
                ScreeningAnswer.conversation_id == conv.id, ScreeningAnswer.superseded_at.is_(None)
            )
        )
    ).scalars()
    answers: dict[str, Any] = {}
    structured: list[StructuredAnswer] = []
    for a in rows:
        q = by_id.get(str(a.question_id))
        if q is None or a.status == "rejected":
            continue
        value = (
            (a.edited_value or a.normalized or {}).get("value") if (a.edited_value or a.normalized) else None
        )
        if value is not None:
            answers[q["key"]] = value
        if a.answer_text:
            structured.append(StructuredAnswer(question_key=q["key"][:60], answer=a.answer_text[:2000]))
    snapshot = [
        {k: v for k, v in q.items() if k in ("key", "label", "type", "options", "required", "maps_to")}
        for q in ver.questions
    ]
    p1_summary = ScreeningSummary(
        structured_answers=structured[:20],
        gaps=[Gap(question_key="gap", issue=g[:500]) for g in gaps[:20]],
        summary=summary[:3000], suggested_outcome=suggested_next,
    )  # fmt: skip
    screening_id = await p1.record_agent_result(
        ctx, db, conv.application_id, questions_snapshot=snapshot, answers=answers, summary=p1_summary,
        source_ref=str(conv.screening_id) if conv.screening_id else "",
    )  # fmt: skip
    conv.screening_id = screening_id
    conv.summary = summary[:3000]
    conv.gaps = gaps[:20]
    conv.suggested_next = suggested_next
    conv.status = "completed"
    conv.completed_at = now()
    name = await channels.owner_name(db, conv)
    turn = await turns.add_turn(
        db,
        conv,
        direction="outbound",
        author="agent",
        kind="closing",
        body=f"Thank you, that's everything I needed. {name} will follow up with you.",
        template_key="closing",
        run_id=run_id,
    )
    await channels.deliver(ctx, db, conv, turn)
    emit(db, _completed_event(conv, "completed"))
    audit.record("screening_conversation.completed", "screening_conversation", conv.id, session=db)
    await db.flush()


async def open_for_candidate(
    db: AsyncSession, candidate_id: uuid.UUID, channel: str
) -> ScreeningConversation | None:
    return (
        await db.execute(
            select(ScreeningConversation)
            .where(
                ScreeningConversation.candidate_id == candidate_id,
                ScreeningConversation.channel == channel,
                ScreeningConversation.status.in_((*OPEN_STATUSES, "taken_over")),
            )
            .order_by(ScreeningConversation.created_at.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()


__all__ = [
    "BLOCK_MESSAGES",
    "alternative_process_requested",
    "cancel",
    "channel_options",
    "complete",
    "conversation_out",
    "end_for_alternative_process",
    "expire",
    "flag",
    "get",
    "grant_consent",
    "handoff",
    "kickoff",
    "list_for_application",
    "open_for_candidate",
    "pause",
    "post_recruiter_message",
    "readable",
    "refuse_consent",
    "resume",
    "start",
    "start_options",
    "start_run",
    "take_over",
    "wake",
    "withdraw",
]
