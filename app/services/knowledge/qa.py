# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Onboarding Q&A (Epic 62 A2, story 62.5, FR-310).

**Portal** (the signed-in Worker / pre-joining Candidate; another person's thread is indistinguishable from a missing
one): eligibility (an open onboarding case or an open Placement, else 409 ``no_onboarding``), the
``knowledge_assistant`` consent gate (409 ``consent_required`` until "Continue"), 20 questions per hour, then the
question is stored and handed to the Onboarding Agent: a waiting run of the thread is resumed with the question as
untrusted text, else a new run starts (``origin="conversation"``, subject ``("onboarding_qa_thread", id)``). When the
agent cannot run (off, kill switch, budget, no owner) the Worker sees "The onboarding assistant is not available. Your
question was sent to your coordinator." and the coordinator gets a Task.

**Agent side** (tools are thread-bound; the asker is resolved from the thread): ``reply_grounded`` validates every
citation (active knowledge chunks for the thread's audience and the asker's legal entity / client, or the asker's own
tasks / case), runs ``grounding.check_answer`` and refuses hand-off topics without a knowledge citation; ``hand_off``
posts "I'll pass this to your coordinator.", creates a Task and feeds the FAQ gap report.

**Internal** (coordinators, ``onboarding:read`` / ``onboarding:manage``): thread inbox, reply (works with AI off),
close. ``grounded_conversation`` is the registered ``onboarding_qa`` grounded-conversation checker.
"""

import dataclasses
import uuid
from datetime import timedelta
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound, RateLimited, ValidationFailed
from app.core.events import emit
from app.core.settings import get_settings
from app.events.knowledge import OnboardingQuestionAsked, OnboardingQuestionHandedOff
from app.models.knowledge import OnboardingQaMessage, OnboardingQaThread
from app.schemas.knowledge import QaCitation, QaMessageOut, QaThreadOut
from app.schemas.knowledge_portal import (
    PortalCitation,
    PortalHelpMessage,
    PortalHelpNotice,
    PortalHelpState,
    PortalHelpThread,
    PortalHelpThreadSummary,
)
from app.services import entity_refs
from app.services.knowledge import grounding, sealing
from app.services.knowledge.common import (
    AGENT_KEY,
    CONSENT_PURPOSE,
    HANDOFF_TEXT,
    HANDOFF_TOPICS,
    QUESTIONS_PER_HOUR,
    UNAVAILABLE_TEXT,
    conflict,
    now,
    user_id,
)

OPEN_PLACEMENT = ("pre_joining", "joining_confirmed", "active")
OPEN_CASE = ("not_started", "in_progress")
AUTHOR = {"asker": "you", "assistant": "assistant", "coordinator": "coordinator", "system": "system"}


@dataclasses.dataclass(frozen=True, slots=True)
class Eligibility:
    audience: str
    placement_id: uuid.UUID | None
    case_id: uuid.UUID | None
    coordinator_id: uuid.UUID | None
    legal_entity_id: uuid.UUID | None
    client_id: uuid.UUID | None


def _sys_ctx(tenant_id: uuid.UUID) -> RequestContext:
    return RequestContext(tenant_id=tenant_id, actor_kind="system")


# ================================================================ eligibility / coordinator
async def eligibility(db: AsyncSession, candidate_id: uuid.UUID) -> Eligibility | None:
    from app.models.onboarding import OnboardingCase, OnboardingSettings
    from app.models.placements import Placement

    placements = list(
        (
            await db.execute(
                select(Placement)
                .where(Placement.candidate_id == candidate_id, Placement.status.in_(OPEN_PLACEMENT))
                .order_by(Placement.planned_start_date.desc())
            )
        ).scalars()
    )
    case = (
        await db.execute(
            select(OnboardingCase)
            .where(OnboardingCase.candidate_id == candidate_id, OnboardingCase.status.in_(OPEN_CASE))
            .order_by(OnboardingCase.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not placements and case is None:
        return None
    placement = next((p for p in placements if case is not None and p.id == case.placement_id), None) or (
        placements[0] if placements else None
    )
    if placement is None and case is not None:
        placement = await db.get(Placement, case.placement_id)
    audience = "worker" if any(p.status == "active" for p in placements) else "candidate"
    coordinator = case.owner_user_id if case is not None else None
    if coordinator is None:
        settings_row = (await db.execute(select(OnboardingSettings).limit(1))).scalar_one_or_none()
        coordinator = settings_row.default_hr_owner_id if settings_row is not None else None
    if coordinator is None and placement is not None:
        coordinator = placement.recruiter_user_id or placement.account_manager_user_id
    if coordinator is not None and coordinator not in await entity_refs.active_user_ids(db, [coordinator]):
        coordinator = None
    return Eligibility(
        audience=audience, placement_id=placement.id if placement else None, case_id=case.id if case else None,
        coordinator_id=coordinator, legal_entity_id=placement.legal_entity_id if placement else None,
        client_id=placement.client_id if placement else None,
    )  # fmt: skip


async def applicability(
    db: AsyncSession, thread: OnboardingQaThread
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    from app.models.placements import Placement

    if thread.placement_id is None:
        return (None, None)
    p = await db.get(Placement, thread.placement_id)
    return (p.legal_entity_id, p.client_id) if p is not None else (None, None)


async def _name(db: AsyncSession, user: uuid.UUID | None, default: str = "your coordinator") -> str:
    info = await entity_refs.user(db, user)
    return info.name if info is not None else default


async def _candidate_name(db: AsyncSession, candidate_id: uuid.UUID) -> str:
    who = await entity_refs.addressee(db, "candidate", candidate_id)
    return (who.full_name if who is not None and who.full_name else None) or "A worker"


# ================================================================ consent
async def has_consent(db: AsyncSession, candidate_id: uuid.UUID) -> bool:
    from app.services.privacy.consent import has_active_consent

    return await has_active_consent(db, candidate_id, CONSENT_PURPOSE)


async def _notice(db: AsyncSession, tenant_id: uuid.UUID) -> PortalHelpNotice:
    from app.services.privacy import consent

    row = None
    try:
        async with db.begin_nested():
            row = await consent.active_notice(db, tenant_id, CONSENT_PURPOSE)
    except Exception:
        row = None
    if row is not None:
        return PortalHelpNotice(notice_id=row.id, title=row.title, body_md=row.body_md)
    title, body = consent.DEFAULT_NOTICES.get(CONSENT_PURPOSE, ("Onboarding help assistant", ""))
    return PortalHelpNotice(notice_id=None, title=title, body_md=body)


async def grant_consent(
    db: AsyncSession, tenant_id: uuid.UUID, candidate_id: uuid.UUID, notice_id: uuid.UUID | None
) -> None:
    from app.services import privacy
    from app.services.privacy.consent import ai_consent_checker

    if await eligibility(db, candidate_id) is None:
        raise conflict("no_onboarding", "Onboarding help is available once your onboarding has started")
    if await has_consent(db, candidate_id):
        return
    ctx = _sys_ctx(tenant_id)
    evidence = {"surface": "onboarding_help"}
    await privacy.grant_consent(
        ctx, db, candidate_id, CONSENT_PURPOSE, source="conversation", notice_id=notice_id, evidence=evidence
    )
    decision = await ai_consent_checker(ctx, db, f"agent_{AGENT_KEY}", ("candidate", candidate_id))
    if not decision.allowed:
        await privacy.grant_consent(
            ctx, db, candidate_id, "ai_processing", source="conversation", evidence=evidence
        )
    await db.flush()


# ================================================================ projections
def _citations(raw: Any) -> list[dict[str, Any]]:
    return [c for c in (raw or []) if isinstance(c, dict)]


def _portal_message(m: OnboardingQaMessage) -> PortalHelpMessage:
    return PortalHelpMessage(
        id=m.id, seq=m.seq, author=AUTHOR.get(m.role, "system"), text=m.plain_text or "",  # type: ignore[arg-type]
        citations=[PortalCitation(label=str(c.get("title") or "Source")[:200], kind=c.get("kind") or "knowledge") for c in _citations(m.citations)],
        feedback=m.feedback, sent_at=m.created_at,  # type: ignore[arg-type]
    )  # fmt: skip


async def _messages(db: AsyncSession, thread_id: uuid.UUID, *, decrypt: bool = True) -> list[OnboardingQaMessage]:
    """The thread's messages; ``decrypt`` fills ``plain_text`` (Epic 78: only for the allowed readers)."""
    msgs = list(
        (
            await db.execute(
                select(OnboardingQaMessage)
                .where(OnboardingQaMessage.thread_id == thread_id)
                .order_by(OnboardingQaMessage.seq)
            )
        ).scalars()
    )
    if decrypt:
        await sealing.open_messages(db, msgs)
    return msgs


async def portal_thread(db: AsyncSession, thread: OnboardingQaThread) -> PortalHelpThread:
    return PortalHelpThread(
        id=thread.id, status=thread.status, coordinator_name=await _name(db, thread.coordinator_user_id),  # type: ignore[arg-type]
        can_reply=thread.status != "closed", messages=[_portal_message(m) for m in await _messages(db, thread.id)],
    )  # fmt: skip


async def _summaries(db: AsyncSession, candidate_id: uuid.UUID) -> list[PortalHelpThreadSummary]:
    threads = list(
        (
            await db.execute(
                select(OnboardingQaThread)
                .where(OnboardingQaThread.candidate_id == candidate_id)
                .order_by(OnboardingQaThread.created_at.desc())
                .limit(50)
            )
        ).scalars()
    )
    out = []
    for t in threads:
        first_msg = (
            await db.execute(
                select(OnboardingQaMessage)
                .where(OnboardingQaMessage.thread_id == t.id, OnboardingQaMessage.role == "asker")
                .order_by(OnboardingQaMessage.seq)
                .limit(1)
            )
        ).scalar_one_or_none()
        first = await sealing.text_of(db, first_msg) if first_msg is not None else None  # the asker's own list
        out.append(
            PortalHelpThreadSummary(
                id=t.id,
                status=t.status,
                first_question=(first or "")[:200],
                last_message_at=t.last_message_at,  # type: ignore[arg-type]
            )
        )
    return out


async def portal_state(db: AsyncSession, tenant_id: uuid.UUID, candidate_id: uuid.UUID) -> PortalHelpState:
    elig = await eligibility(db, candidate_id)
    consented = await has_consent(db, candidate_id)
    return PortalHelpState(
        available=elig is not None, consent_needed=elig is not None and not consented,
        coordinator_name=await _name(db, elig.coordinator_id if elig else None),
        notice=await _notice(db, tenant_id) if elig is not None and not consented else None,
        threads=await _summaries(db, candidate_id),
    )  # fmt: skip


async def own_thread(
    db: AsyncSession, candidate_id: uuid.UUID, thread_id: uuid.UUID, *, lock: bool = False
) -> OnboardingQaThread:
    stmt = select(OnboardingQaThread).where(
        OnboardingQaThread.id == thread_id, OnboardingQaThread.candidate_id == candidate_id
    )
    if lock:
        stmt = stmt.with_for_update()
    thread = (await db.execute(stmt)).scalar_one_or_none()
    if thread is None:
        raise NotFound("Thread not found")
    return thread


# ================================================================ messages
async def add_message(
    db: AsyncSession,
    thread: OnboardingQaThread,
    role: str,
    text: str,
    *,
    citations: list[dict[str, Any]] | None = None,
    outcome: str | None = None,
    topic: str | None = None,
    run_id: uuid.UUID | None = None,
    author_user_id: uuid.UUID | None = None,
) -> OnboardingQaMessage:
    seq = (
        await db.execute(
            select(func.coalesce(func.max(OnboardingQaMessage.seq), 0)).where(
                OnboardingQaMessage.thread_id == thread.id
            )
        )
    ).scalar_one()
    # Epic 78 (FR-404): written sealed only (the AAD binds the row id, so the id is generated first).
    message_id = uuid.uuid4()
    body = text[:2000]
    msg = OnboardingQaMessage(
        id=message_id, tenant_id=thread.tenant_id, thread_id=thread.id, candidate_id=thread.candidate_id,
        seq=int(seq) + 1, role=role, text_sealed=await sealing.seal_message(db, thread.tenant_id, message_id, body),
        citations=citations or [], outcome=outcome, topic=topic, agent_run_id=run_id, author_user_id=author_user_id,
    )  # fmt: skip
    msg.plain_text = body
    if role == "asker":
        msg.question_fingerprint = sealing.fingerprint(thread.tenant_id, body)
        msg.topic_label = sealing.topic_label(topic, body)
    elif topic:
        await _label_question(db, thread, topic)
    db.add(msg)
    thread.last_message_at = now()
    await db.flush()
    return msg


async def _label_question(db: AsyncSession, thread: OnboardingQaThread, topic: str) -> None:
    """The answer's topic becomes the prefix of the latest question's ``topic_label`` (non-reversible label)."""
    last = (
        await db.execute(
            select(OnboardingQaMessage)
            .where(OnboardingQaMessage.thread_id == thread.id, OnboardingQaMessage.role == "asker")
            .order_by(OnboardingQaMessage.seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if last is not None:
        last.topic_label = sealing.relabel(last.topic_label, topic)


async def _rate_limit(db: AsyncSession, candidate_id: uuid.UUID) -> None:
    since = now() - timedelta(hours=1)
    count = (
        await db.execute(
            select(func.count())
            .select_from(OnboardingQaMessage)
            .where(
                OnboardingQaMessage.candidate_id == candidate_id,
                OnboardingQaMessage.role == "asker",
                OnboardingQaMessage.created_at >= since,
            )
        )
    ).scalar_one()
    if int(count) >= QUESTIONS_PER_HOUR:
        raise RateLimited(
            "You have asked a lot of questions in the last hour. Please try again later.",
            code="rate_limited",
            details={"limit_per_hour": QUESTIONS_PER_HOUR},
        )


async def ask(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    text: str,
    thread_id: uuid.UUID | None = None,
) -> PortalHelpThread:
    elig = await eligibility(db, candidate_id)
    if elig is None:
        raise conflict("no_onboarding", "Onboarding help is available once your onboarding has started")
    if not await has_consent(db, candidate_id):
        raise conflict("consent_required", 'Please read "Before you ask" and choose "Continue" first')
    clean = " ".join(text.split())[:2000]
    if not clean:
        raise ValidationFailed("Type your question", code="question_required")
    await _rate_limit(db, candidate_id)
    if thread_id is None:
        thread = OnboardingQaThread(
            id=uuid.uuid4(), tenant_id=tenant_id, candidate_id=candidate_id, onboarding_case_id=elig.case_id,
            placement_id=elig.placement_id, audience=elig.audience, status="open", coordinator_user_id=elig.coordinator_id,
        )  # fmt: skip
        db.add(thread)
        await db.flush()
        new = True
    else:
        thread = await own_thread(db, candidate_id, thread_id, lock=True)
        if thread.status == "closed":
            raise conflict("thread_closed", "This conversation is closed. Start a new question.")
        thread.status = "open"
        new = False
    msg = await add_message(db, thread, "asker", clean)
    emit(
        db,
        OnboardingQuestionAsked(
            tenant_id=tenant_id, thread_id=thread.id, candidate_id=candidate_id, message_id=msg.id
        ),
    )
    await _dispatch(db, thread, msg, clean, new=new)
    return await portal_thread(db, thread)


async def _dispatch(
    db: AsyncSession, thread: OnboardingQaThread, msg: OnboardingQaMessage, text: str, *, new: bool
) -> None:
    from app.core.agentic import Kickoff
    from app.services import agent_runtime_service as runtime

    ctx = _sys_ctx(thread.tenant_id)
    if not new:
        refs = await runtime.resume(ctx, db, condition="reply", ref=thread.id, untrusted_text=text)
        if refs:
            return
    if thread.coordinator_user_id is None:
        await unavailable(db, thread, "owner_unavailable")
        return
    tenant = await entity_refs.tenant(db, thread.tenant_id)
    kickoff = Kickoff(
        inputs={"thread_id": str(thread.id), "audience": thread.audience, "tenant_name": tenant.name[:120]},
        untrusted={"question": text},
        candidate_ids=(thread.candidate_id,),
    )
    try:
        async with db.begin_nested():
            ref = await runtime.start_run(
                ctx, db, agent_key=AGENT_KEY, trigger_kind="inbound", trigger_key="question",
                subject=("onboarding_qa_thread", thread.id), kickoff=kickoff, owner_user_id=thread.coordinator_user_id,
                dedup_key=f"thread:{thread.id}:{msg.seq}", conversation_id=thread.id, origin="conversation",
            )  # fmt: skip
    except Exception:
        logger.exception("Onboarding Q&A run could not start for thread {}", thread.id)
        ref = None
    if ref is None or ref.run_id is None:
        await unavailable(db, thread, getattr(ref, "reason", None) or "not_started")
        return
    thread.agent_run_id = ref.run_id
    await db.flush()


async def unavailable(db: AsyncSession, thread: OnboardingQaThread, reason: str) -> None:
    """Tell the asker the agent cannot answer and hand the question to the coordinator (works with AI off)."""
    await add_message(db, thread, "system", UNAVAILABLE_TEXT, outcome="handed_off")
    await _handoff_task(db, thread, reason, None, outcome_reason="unavailable")


# ================================================================ hand-off
async def _handoff_task(
    db: AsyncSession, thread: OnboardingQaThread, reason: str, topic: str | None, *, outcome_reason: str
) -> uuid.UUID | None:
    from app.schemas.tasks import TaskCreate
    from app.services import tasks_service

    thread.status = "handed_off"
    owner = thread.coordinator_user_id
    if owner is None:
        owner = await _fallback_owner(db)
    task_id = None
    if owner is not None:
        name = await _candidate_name(db, thread.candidate_id)
        data = TaskCreate.model_construct(
            title=f"Onboarding question from {name}"[:200],
            notes=f"Open the onboarding question inbox: /onboarding/questions?thread={thread.id} (reason: {outcome_reason}).",
            owner_id=owner, entity_type="candidate", entity_id=thread.candidate_id,
            due_at=now() + timedelta(hours=24), due_all_day=False, priority="normal", reminder_at=None,
        )  # fmt: skip
        try:
            async with db.begin_nested():
                task = await tasks_service.create_system_task(
                    _sys_ctx(thread.tenant_id), db, data, source="onboarding"
                )
            task_id = task.id
            thread.task_id = task.id
        except Exception:
            logger.exception("Could not create the hand-off task of onboarding thread {}", thread.id)
    await db.flush()
    if task_id is not None and owner is not None:
        from app.services import notifications_service as ns

        try:
            await ns.deliver(
                db, "onboarding_question_handoff", [owner],
                ns.Content(title=f"{await _candidate_name(db, thread.candidate_id)} asked a question"[:300],
                           entity_type="candidate", entity_id=thread.candidate_id,
                           link_path=f"/onboarding/questions?thread={thread.id}"),
                event_id=f"onb_handoff:{thread.id}:{task_id}",
            )  # fmt: skip
        except Exception:
            logger.exception("onboarding_question_handoff not delivered for thread {}", thread.id)
    if task_id is not None:
        emit(
            db,
            OnboardingQuestionHandedOff(
                tenant_id=thread.tenant_id, thread_id=thread.id, candidate_id=thread.candidate_id, task_id=task_id,
                topic=topic, reason=reason,
            ),
        )  # fmt: skip
    return task_id


async def _fallback_owner(db: AsyncSession) -> uuid.UUID | None:
    ids = await entity_refs.role_user_ids(db, "hr_onboarding") or await entity_refs.role_user_ids(
        db, "tenant_admin"
    )
    active = await entity_refs.active_user_ids(db, ids)
    return sorted(active)[0] if active else None


async def hand_off(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    thread: OnboardingQaThread,
    reason: str,
    topic: str | None,
    run_id: uuid.UUID | None,
) -> uuid.UUID | None:
    outcome = "ungrounded" if reason == "ungrounded" else "handed_off"
    await add_message(db, thread, "system", HANDOFF_TEXT, outcome=outcome, topic=topic, run_id=run_id)
    return await _handoff_task(db, thread, reason, topic, outcome_reason=reason)


async def request_person(
    db: AsyncSession, candidate_id: uuid.UUID, thread_id: uuid.UUID | None, tenant_id: uuid.UUID | None = None
) -> PortalHelpThread:
    """ "Talk to my coordinator": works without consent and with AI off (a new thread when ``thread_id`` is None)."""
    if thread_id is None:
        elig = await eligibility(db, candidate_id)
        if elig is None or tenant_id is None:
            raise conflict("no_onboarding", "Onboarding help is available once your onboarding has started")
        thread = OnboardingQaThread(
            id=uuid.uuid4(), tenant_id=tenant_id, candidate_id=candidate_id, onboarding_case_id=elig.case_id,
            placement_id=elig.placement_id, audience=elig.audience, status="open", coordinator_user_id=elig.coordinator_id,
        )  # fmt: skip
        db.add(thread)
        await db.flush()
    else:
        thread = await own_thread(db, candidate_id, thread_id, lock=True)
    if thread.status == "closed":
        raise conflict("thread_closed", "This conversation is closed")
    await add_message(db, thread, "asker", "I would like to talk to my coordinator.")
    await add_message(db, thread, "system", HANDOFF_TEXT, outcome="handed_off")
    await _handoff_task(db, thread, "asked_for_person", None, outcome_reason="asked_for_person")
    return await portal_thread(db, thread)


async def feedback(
    db: AsyncSession, candidate_id: uuid.UUID, message_id: uuid.UUID, value: str, note: str | None
) -> PortalHelpThread:
    msg = (
        await db.execute(
            select(OnboardingQaMessage).where(
                OnboardingQaMessage.id == message_id, OnboardingQaMessage.candidate_id == candidate_id
            )
        )
    ).scalar_one_or_none()
    if msg is None or msg.role not in ("assistant", "coordinator"):
        raise NotFound("Message not found")
    msg.feedback = value
    clean_note = (note or "").strip()[:500]
    msg.feedback_note_sealed = await sealing.seal_note(db, msg.tenant_id, msg.id, clean_note) if clean_note else None
    await db.flush()
    thread = await own_thread(db, candidate_id, msg.thread_id)
    return await portal_thread(db, thread)


# ================================================================ agent side
async def thread_for_run(db: AsyncSession, run_id: uuid.UUID) -> OnboardingQaThread | None:
    thread = (
        await db.execute(select(OnboardingQaThread).where(OnboardingQaThread.agent_run_id == run_id).limit(1))
    ).scalar_one_or_none()
    if thread is not None:
        return thread
    from app.models.agent_runtime import AgentRun

    run = await db.get(AgentRun, run_id)
    if run is None or run.subject_type != "onboarding_qa_thread" or run.subject_id is None:
        return None
    return await db.get(OnboardingQaThread, run.subject_id)


async def load_for_tool(
    db: AsyncSession, thread_id: uuid.UUID, subject: tuple[str, uuid.UUID] | None, *, lock: bool = False
) -> OnboardingQaThread:
    """Load the thread of the run subject only; any other id -> ``not_found``."""
    if subject is None or subject[0] != "onboarding_qa_thread" or subject[1] != thread_id:
        raise NotFound("Thread not found")
    stmt = select(OnboardingQaThread).where(OnboardingQaThread.id == thread_id)
    if lock:
        stmt = stmt.with_for_update()
    thread = (await db.execute(stmt)).scalar_one_or_none()
    if thread is None:
        raise NotFound("Thread not found")
    return thread


async def tool_thread_view(db: AsyncSession, thread: OnboardingQaThread) -> dict[str, Any]:
    msgs = (await _messages(db, thread.id))[-10:]
    tenant = await entity_refs.tenant(db, thread.tenant_id)
    return {
        "audience": thread.audience,
        "status": thread.status,
        "messages": [{"seq": m.seq, "role": m.role, "text": (m.plain_text or "")[:1500]} for m in msgs],
        "coordinator_name": await _name(db, thread.coordinator_user_id),
        "tenant_name": tenant.name,
    }


async def _case_rows(db: AsyncSession, thread: OnboardingQaThread) -> tuple[Any, list[Any], Any]:
    from app.models.onboarding import OnboardingCase, OnboardingTask
    from app.models.placements import Placement

    case = None
    if thread.onboarding_case_id is not None:
        case = await db.get(OnboardingCase, thread.onboarding_case_id)
    if case is None:
        case = (
            await db.execute(
                select(OnboardingCase)
                .where(OnboardingCase.candidate_id == thread.candidate_id)
                .order_by(OnboardingCase.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if case is not None and case.candidate_id != thread.candidate_id:
        case = None
    tasks = (
        list(
            (
                await db.execute(
                    select(OnboardingTask)
                    .where(
                        OnboardingTask.case_id == case.id,
                        OnboardingTask.candidate_id == thread.candidate_id,
                        OnboardingTask.assignee == "worker",
                    )
                    .order_by(OnboardingTask.position)
                )
            ).scalars()
        )
        if case is not None
        else []
    )
    placement = await db.get(Placement, thread.placement_id) if thread.placement_id else None
    if placement is not None and placement.candidate_id != thread.candidate_id:
        placement = None
    return case, tasks, placement


async def my_case(db: AsyncSession, thread: OnboardingQaThread) -> dict[str, Any]:
    """Return the asker's own case, worker tasks and Placement basics -- never bank, tax or identity values."""
    case, tasks, placement = await _case_rows(db, thread)
    client_name = None
    if placement is not None:
        client_name = await entity_refs.entity_label(db, "client", placement.client_id)
    return {
        "case": {
            "case_id": str(case.id),
            "status": case.status,
            "progress_pct": case.progress_pct,
            "start_date": placement.planned_start_date.isoformat() if placement else None,
        }
        if case is not None
        else None,
        "tasks": [
            {
                "task_id": str(t.id),
                "label": t.label,
                "instructions": (t.instructions or "")[:1000],
                "status": t.status,
                "due_on": t.due_on.isoformat() if t.due_on else None,
            }
            for t in tasks
        ],
        "placement": {
            "client_name": client_name,
            "start_date": placement.planned_start_date.isoformat(),
            "work_location": placement.work_location_text,
        }
        if placement is not None
        else None,
    }


def _record_facts(case: Any, tasks: list[Any], placement: Any) -> list[str]:
    facts: list[str] = []
    if case is not None:
        facts.append(f"Onboarding case status {case.status}, {case.progress_pct}% complete")
    if placement is not None:
        facts.append(f"Start date {placement.planned_start_date.isoformat()}")
        if placement.work_location_text:
            facts.append(f"Work location {placement.work_location_text}")
    for t in tasks:
        line = f"Task {t.label}: status {t.status}"
        if t.due_on:
            line += f", due {t.due_on.isoformat()}"
        facts.append(line)
        if t.instructions:
            facts.append(t.instructions[:1000])
    return facts


async def reply_grounded(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    thread: OnboardingQaThread,
    answer: str,
    citations: list[str],
    topic: str | None,
    run_id: uuid.UUID | None,
) -> OnboardingQaMessage:
    from app.services.knowledge.service import active_chunks, parse_citation

    if thread.status == "closed":
        raise conflict("thread_closed", "The conversation is closed")
    msgs = await _messages(db, thread.id)
    last_asker = max((m.seq for m in msgs if m.role == "asker"), default=0)
    if any(m.seq > last_asker and m.role in ("assistant", "coordinator") for m in msgs):
        raise conflict("already_answered", "The latest question already has an answer")
    parsed = [parse_citation(c) for c in citations]
    if any(p is None for p in parsed):
        raise ValidationFailed("Unknown citation", code="invalid_citation")
    case, tasks, placement = await _case_rows(db, thread)
    le, client = (placement.legal_entity_id, placement.client_id) if placement is not None else (None, None)
    kb_ids = [p[1] for p in parsed if p is not None and p[0] == "kb"]
    chunks = await active_chunks(db, kb_ids)
    out_citations: list[dict[str, Any]] = []
    cited_texts: list[str] = []
    for p in parsed:
        assert p is not None
        kind, ref = p
        if kind == "kb":
            found = chunks.get(ref)
            if found is None:
                raise NotFound("Source not found")
            chunk, title = found
            if chunk.audience not in (thread.audience, "both"):
                raise NotFound("Source not found")
            if (chunk.legal_entity_id is not None and chunk.legal_entity_id != le) or (
                chunk.client_id is not None and chunk.client_id != client
            ):
                raise NotFound("Source not found")
            cited_texts.append(f"{chunk.heading or ''}\n{chunk.text}")
            out_citations.append({"citation": f"kb:{ref}", "title": title[:200], "kind": "knowledge"})
        elif kind == "task":
            task = next((t for t in tasks if t.id == ref), None)
            if task is None:
                raise NotFound("Task not found")
            out_citations.append({"citation": f"task:{ref}", "title": task.label[:200], "kind": "task"})
        else:
            if case is None or case.id != ref:
                raise NotFound("Case not found")
            out_citations.append({"citation": f"case:{ref}", "title": "Your onboarding", "kind": "case"})
    if topic in HANDOFF_TOPICS and not kb_ids:
        raise ValidationFailed("Hand this question to the coordinator", code="handoff_required")
    result = grounding.check_answer(answer, cited_texts, _record_facts(case, tasks, placement))
    if not result.ok:
        raise ValidationFailed(
            "The answer states something the cited sources do not support",
            code="ungrounded_claim",
            details={"types": list(result.violations)},
        )
    from app.services.agent_runtime.sanitize import outbound

    clean = outbound(answer, max_len=1500).text
    owner = await _name(db, thread.coordinator_user_id)
    disclosure = get_settings().agent_disclosure_line.format(owner_name=owner)
    msg = await add_message(
        db, thread, "assistant", f"{clean}\n\n{disclosure}", citations=out_citations, outcome="answered",
        topic=topic, run_id=run_id,
    )  # fmt: skip
    thread.status = "answered"
    await db.flush()
    await _notify_answer(db, thread)
    return msg


async def _notify_answer(db: AsyncSession, thread: OnboardingQaThread) -> None:
    from app.services import notifications_service as ns

    try:
        await ns.deliver(
            db, "onboarding_answer_ready", [ns.Recipient("candidate", thread.candidate_id)],
            ns.Content(title="Your onboarding question has an answer", link_path=f"/portal/work/help/{thread.id}"),
            event_id=f"onb_answer:{thread.id}:{thread.last_message_at.isoformat() if thread.last_message_at else ''}",
            payload={"thread_id": str(thread.id)},
        )  # fmt: skip
    except Exception:
        logger.exception("onboarding_answer_ready could not be delivered for thread {}", thread.id)


async def grounded_conversation(ctx: RequestContext, db: AsyncSession, conversation_id: uuid.UUID) -> bool:  # ruff: ignore[unused-function-argument]
    """``onboarding_qa`` checker: the thread exists, is open / answered, was started by its candidate and the asker
    accepted ``knowledge_assistant``."""
    thread = await db.get(OnboardingQaThread, conversation_id)
    if thread is None or thread.status not in ("open", "answered"):
        return False
    first = (
        await db.execute(
            select(OnboardingQaMessage.role)
            .where(OnboardingQaMessage.thread_id == thread.id)
            .order_by(OnboardingQaMessage.seq)
            .limit(1)
        )
    ).scalar_one_or_none()
    return first == "asker" and await has_consent(db, thread.candidate_id)


async def run_failed(db: AsyncSession, run_id: uuid.UUID, reason: str) -> bool:
    """``AgentRunFailed`` for an onboarding run: the asker sees the hand-off line and the coordinator gets a Task."""
    thread = await thread_for_run(db, run_id)
    if thread is None or thread.status in ("closed", "handed_off"):
        return False
    msgs = await _messages(db, thread.id)
    last_asker = max((m.seq for m in msgs if m.role == "asker"), default=0)
    if any(m.seq > last_asker and m.role in ("assistant", "coordinator", "system") for m in msgs):
        return False
    await add_message(db, thread, "system", HANDOFF_TEXT, outcome="handed_off", run_id=run_id)
    await _handoff_task(db, thread, reason[:40], None, outcome_reason=f"run_failed:{reason}")
    return True


# ================================================================ internal (coordinators)
def _message_out(m: OnboardingQaMessage) -> QaMessageOut:
    return QaMessageOut(
        id=m.id, seq=m.seq, role=m.role, text=m.plain_text or "",  # type: ignore[arg-type]
        citations=[QaCitation(citation=str(c.get("citation") or ""), title=str(c.get("title") or ""), kind=c.get("kind") or "knowledge") for c in _citations(m.citations)],
        outcome=m.outcome, topic=m.topic, feedback=m.feedback, agent_run_id=m.agent_run_id,  # type: ignore[arg-type]
        author_user_id=m.author_user_id, created_at=m.created_at,
    )  # fmt: skip


async def _thread_out(db: AsyncSession, t: OnboardingQaThread, *, with_messages: bool) -> QaThreadOut:
    # Epic 78: list views show the non-reversible ``topic_label``; only the thread view decrypts.
    msgs = await _messages(db, t.id, decrypt=with_messages)
    last_asker = next((m for m in reversed(msgs) if m.role == "asker"), None)
    last_q = (last_asker.plain_text if with_messages else last_asker.topic_label) if last_asker is not None else None
    return QaThreadOut(
        id=t.id, candidate_id=t.candidate_id, candidate_name=await _candidate_name(db, t.candidate_id),
        audience=t.audience, status=t.status, coordinator_user_id=t.coordinator_user_id,  # type: ignore[arg-type]
        coordinator_name=await _name(db, t.coordinator_user_id, "") or None, placement_id=t.placement_id, task_id=t.task_id,
        last_message_at=t.last_message_at, created_at=t.created_at, last_question=(last_q or "")[:300] or None,
        messages=[_message_out(m) for m in msgs] if with_messages else [],
    )  # fmt: skip


async def _visible(ctx: RequestContext, db: AsyncSession, t: OnboardingQaThread) -> bool:
    return t.coordinator_user_id == user_id(ctx) or await entity_refs.can_read(
        ctx, db, "candidate", t.candidate_id
    )


async def list_internal(
    ctx: RequestContext, db: AsyncSession, *, view: str = "all", limit: int = 100
) -> list[QaThreadOut]:
    """List threads: ``waiting`` = waiting for me (open / handed off, I coordinate); ``mine``; ``answered`` (by the assistant or a
    person); ``all``. Rows outside the caller's candidate visibility are dropped."""
    stmt = (
        select(OnboardingQaThread).order_by(OnboardingQaThread.last_message_at.desc().nulls_last()).limit(300)
    )
    me = user_id(ctx)
    if view == "waiting":
        stmt = stmt.where(
            OnboardingQaThread.status.in_(("handed_off", "open")),
            OnboardingQaThread.coordinator_user_id == me,
        )
    elif view == "mine":
        stmt = stmt.where(OnboardingQaThread.coordinator_user_id == me)
    elif view == "answered":
        stmt = stmt.where(OnboardingQaThread.status == "answered")
    out: list[QaThreadOut] = []
    for t in (await db.execute(stmt)).scalars():
        if await _visible(ctx, db, t):
            out.append(await _thread_out(db, t, with_messages=False))
        if len(out) >= limit:
            break
    return out


async def _internal_thread(
    ctx: RequestContext, db: AsyncSession, thread_id: uuid.UUID, *, lock: bool = False
) -> OnboardingQaThread:
    stmt = select(OnboardingQaThread).where(OnboardingQaThread.id == thread_id)
    if lock:
        stmt = stmt.with_for_update()
    t = (await db.execute(stmt)).scalar_one_or_none()
    if t is None or not await _visible(ctx, db, t):
        raise NotFound("Thread not found")
    return t


async def get_internal(ctx: RequestContext, db: AsyncSession, thread_id: uuid.UUID) -> QaThreadOut:
    return await _thread_out(db, await _internal_thread(ctx, db, thread_id), with_messages=True)


async def coordinator_reply(
    ctx: RequestContext, db: AsyncSession, thread_id: uuid.UUID, text: str
) -> QaThreadOut:
    from app.services.agent_runtime.sanitize import outbound

    t = await _internal_thread(ctx, db, thread_id, lock=True)
    if t.status == "closed":
        raise conflict("thread_closed", "The conversation is closed")
    clean = outbound(text, max_len=1500).text
    await add_message(db, t, "coordinator", clean, author_user_id=user_id(ctx))
    t.status = "answered"
    await db.flush()
    await _notify_answer(db, t)
    return await _thread_out(db, t, with_messages=True)


async def close(ctx: RequestContext, db: AsyncSession, thread_id: uuid.UUID) -> QaThreadOut:
    t = await _internal_thread(ctx, db, thread_id, lock=True)
    t.status = "closed"
    await db.flush()
    return await _thread_out(db, t, with_messages=True)


__all__ = [
    "Eligibility",
    "applicability",
    "ask",
    "close",
    "coordinator_reply",
    "eligibility",
    "feedback",
    "get_internal",
    "grant_consent",
    "grounded_conversation",
    "hand_off",
    "has_consent",
    "list_internal",
    "load_for_tool",
    "my_case",
    "own_thread",
    "portal_state",
    "portal_thread",
    "reply_grounded",
    "request_person",
    "run_failed",
    "thread_for_run",
    "tool_thread_view",
    "unavailable",
]


async def thread_owner(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    subject: tuple[str, uuid.UUID] | None,
) -> uuid.UUID | None:
    """Return the Accountable Owner of an onboarding run: the thread's coordinator (case owner -> default HR owner)."""
    if subject is None or subject[0] != "onboarding_qa_thread":
        return None
    thread = await db.get(OnboardingQaThread, subject[1])
    return thread.coordinator_user_id if thread is not None else None


__all__ += ["thread_owner"]
