# ruff: file-ignore[line-too-long]
"""Operations behind the Screening Agent tools (Epic 57 Part B). Tools (``agent_tools/screening``) call these through
the ``screening_agent_service`` facade.

Every operation re-loads the conversation and requires it to belong to the run subject (the Application): another
conversation's id is indistinguishable from a missing one (404). Writes need an open conversation (409
``conversation_closed``). Texts sent to the Candidate are server-rendered from approved content; the only model text is
a Grounded Answer restating cited posting fields (commitment validator).
"""

import hashlib
import json
import re
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound
from app.models.screening_agent import ScreeningConversation
from app.services.screening_agent import channels, conversations, grounding, turns
from app.services.screening_agent.common import (
    conflict,
    conversation,
    invalid,
    is_open,
    now,
    required_ids,
    settings_row,
    version,
    wa_window_open,
)
from app.services.screening_agent.questionnaires import DEFAULT_CLARIFICATIONS

ACCEPTED = ("consented", "in_progress")
SUMMARY_BLOCKLIST = re.compile(
    r"\b(tone|tonality|personality|emotion\w*|emotional|nervous\w*|anxious|confident|confidence level|enthusias\w*|"
    r"friendly|rude|polite|aggressive|accent|fluency|fluent|articulate|attitude|temperament|mood|"
    r"age[ds]?|old|young|elderly|gender|male|female|man|woman|married|marital|pregnan\w*|religio\w*|caste|"
    r"ethnic\w*|race|racial|nationality|disab\w*|health|sexual\w*|family plans?|children)\b",
    re.IGNORECASE,
)


async def load(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    subject: tuple[str, uuid.UUID] | None,
    *,
    write: bool = False,
) -> ScreeningConversation:
    conv = await conversation(db, conversation_id, for_update=write)
    if subject is None or subject[0] != "application" or conv.application_id != subject[1]:
        raise NotFound("Screening conversation not found")
    if write and not is_open(conv):
        raise conflict("conversation_closed", "This conversation is no longer open", status=conv.status)
    return conv


def recipient_channel(conv: ScreeningConversation) -> str:
    return {"portal_chat": "portal", "link_chat": "portal", "email": "email", "whatsapp": "whatsapp"}[
        conv.channel
    ]


# ================================================================ reads
async def questionnaire_view(db: AsyncSession, conv: ScreeningConversation) -> dict[str, Any]:
    from app.services.demands import pipeline_port as demands_port
    from app.services.screening_agent import answers as answers_mod

    ver = await version(db, conv.questionnaire_version_id)
    current = await answers_mod.current(db, conv.transcript_id) if conv.transcript_id else []
    clar = {**DEFAULT_CLARIFICATIONS, **dict(ver.clarifications or {})}
    types = {q["type"] for q in ver.questions}
    skills = await demands_port.skills(db, conv.demand_id)
    return {
        "version_id": ver.id,
        "questions": [
            {
                "id": q["id"], "label": q["label"], "type": q["type"], "unit": q.get("unit"),
                "options": q.get("options"), "required": bool(q.get("required")), "knockout": q.get("knockout"),
            }
            for q in ver.questions
        ],
        "answered": [
            {"question_id": a.question_id, "normalized": a.normalized, "confidence": float(a.confidence or 0)}
            for a in current
            if a.status != "rejected"
        ],
        "clarification_keys": {t: sorted((clar.get(t) or {}).keys()) for t in sorted(types)},
        "demand_requirements": [{"label": s["label"], "must_have": s["kind"] == "mandatory"} for s in skills][:30],
    }  # fmt: skip


async def contact_channels(db: AsyncSession, conv: ScreeningConversation) -> dict[str, Any]:
    from app.services import entity_refs
    from app.services.messaging import opt_outs
    from app.services.messaging.sending import has_whatsapp_consent, resolve_phone

    who = await entity_refs.addressee(db, "candidate", conv.candidate_id)
    phone = await resolve_phone(db, "candidate", conv.candidate_id)
    wa_in = (
        bool(phone)
        and not await opt_outs.is_opted_out(db, "whatsapp", phone)
        and await has_whatsapp_consent(db, "candidate", conv.candidate_id)
    )  # type: ignore[arg-type]
    sms = bool(phone) and not await opt_outs.is_opted_out(db, "sms", phone)  # type: ignore[arg-type]
    return {
        "portal": await conversations._portal_active(conv.candidate_id),
        "email": who is not None and bool(who.address),
        "whatsapp": {"opted_in": wa_in, "window_open": wa_window_open(conv)},
        "sms": sms,
    }


async def postings(db: AsyncSession, conv: ScreeningConversation) -> list[dict[str, Any]]:
    return await grounding.posting_fields(db, conv.demand_id)


# ================================================================ sends
async def invite(
    ctx: RequestContext, db: AsyncSession, conv: ScreeningConversation, run_id: uuid.UUID | None
) -> dict[str, Any]:
    """Store the disclosure turn and send the templated invitation (idempotent: once per conversation)."""
    if conv.status != "invited":
        raise conflict("already_invited", "The candidate was already invited")
    existing = [t for t in await turns.list_turns(db, conv.id) if t.kind == "disclosure"]
    if existing:
        return {"turn_seq": existing[0].seq, "delivered": True}
    name = await channels.owner_name(db, conv)
    company = await channels.company_name(db, conv)
    body = (
        turns.disclosure_text_chat(name, company)
        if conv.channel in ("portal_chat", "link_chat")
        else turns.disclosure_text(name, company, None)
    )
    turn = await turns.add_turn(
        db,
        conv,
        direction="outbound",
        author="agent",
        kind="disclosure",
        body=body,
        template_key="ai_agent_communication_disclosure",
        run_id=run_id,
    )
    delivered = await channels.send_invitation(ctx, db, conv, turn)
    return {"turn_seq": turn.seq, "delivered": delivered}


def _require_consent(conv: ScreeningConversation) -> None:
    if conv.status not in ACCEPTED:
        raise invalid("consent_pending", "The candidate has not agreed to chat with the AI assistant yet")


def _window(conv: ScreeningConversation) -> None:
    if conv.channel == "whatsapp" and not wa_window_open(conv):
        raise invalid(
            "window_closed", "The WhatsApp session window is closed; wait for the candidate's next message"
        )


async def ask(
    ctx: RequestContext,
    db: AsyncSession,
    conv: ScreeningConversation,
    question_ids: list[uuid.UUID],
    run_id: uuid.UUID | None,
) -> dict[str, Any]:
    _require_consent(conv)
    _window(conv)
    ver = await version(db, conv.questionnaire_version_id)
    by_id = {q["id"]: q for q in ver.questions}
    chosen = [by_id.get(str(q)) for q in question_ids]
    if any(q is None for q in chosen):
        raise invalid("unknown_question", "Ask only questions of the approved questionnaire")
    settings = await settings_row(db)
    if len(chosen) > settings.questions_per_turn:
        raise invalid("too_many_questions", f"Ask at most {settings.questions_per_turn} questions at a time")
    body = turns.render_questions(chosen)  # type: ignore[arg-type]
    turn = await turns.add_turn(
        db,
        conv,
        direction="outbound",
        author="agent",
        kind="question",
        body=body,
        question_ids=question_ids,
        citations=[f"question:{q}" for q in question_ids],
        run_id=run_id,
    )
    conv.status = "in_progress"
    delivered = await channels.deliver(ctx, db, conv, turn)
    return {"turn_seq": turn.seq, "delivered": delivered or conv.channel != "email"}


async def clarify(
    ctx: RequestContext,
    db: AsyncSession,
    conv: ScreeningConversation,
    question_id: uuid.UUID,
    template_key: str,
    run_id: uuid.UUID | None,
) -> dict[str, Any]:
    _require_consent(conv)
    _window(conv)
    ver = await version(db, conv.questionnaire_version_id)
    q = next((x for x in ver.questions if x["id"] == str(question_id)), None)
    if q is None:
        raise invalid("unknown_question", "Clarify only questions of the approved questionnaire")
    templates = {**DEFAULT_CLARIFICATIONS, **dict(ver.clarifications or {})}.get(q["type"]) or {}
    text = templates.get(template_key)
    if text is None:
        raise invalid("unknown_template", "Use an approved clarification template for this question type")
    body = f'About "{q["label"]}": {text}'
    turn = await turns.add_turn(
        db,
        conv,
        direction="outbound",
        author="agent",
        kind="clarification",
        body=body,
        question_ids=[question_id],
        citations=[f"clarification:{template_key}"],
        template_key=template_key,
        run_id=run_id,
    )
    await channels.deliver(ctx, db, conv, turn)
    return {"turn_seq": turn.seq}


async def reply_grounded(
    ctx: RequestContext,
    db: AsyncSession,
    conv: ScreeningConversation,
    text: str,
    cited_fields: list[str],
    run_id: uuid.UUID | None,
) -> dict[str, Any]:
    from app.services.agent_runtime.sanitize import outbound

    _require_consent(conv)
    _window(conv)
    fields = {f["key"]: f for f in await grounding.posting_fields(db, conv.demand_id)}
    unknown = [f for f in cited_fields if f not in fields]
    if unknown:
        raise invalid("ungrounded", "Cite only published posting fields", fields=unknown)
    clean = outbound(text).text.strip()
    if not clean:
        raise invalid("empty_text", "The answer is empty")
    commitment = grounding.check_reply_text(clean)
    if commitment:
        raise invalid("commitment", "A grounded answer cannot make promises, commitments or mention amounts")
    sources = ", ".join(fields[f]["label"] for f in cited_fields)
    turn = await turns.add_turn(
        db,
        conv,
        direction="outbound",
        author="agent",
        kind="grounded_answer",
        body=f"{clean}\n(Source: {sources})",
        citations=[f"posting:{f}" for f in cited_fields],
        run_id=run_id,
    )
    await channels.deliver(ctx, db, conv, turn)
    return {"turn_seq": turn.seq}


async def extract(
    ctx: RequestContext, db: AsyncSession, conv: ScreeningConversation, run_id: uuid.UUID | None
) -> dict[str, Any]:
    from app.schemas.screening_agent import ExtractAnswersIn
    from app.services.screening_agent.service import extract_answers

    _require_consent(conv)
    ver = await version(db, conv.questionnaire_version_id)
    transcript_id = await turns.ensure_transcript(db, conv)
    out = await extract_answers(
        ctx, db,
        ExtractAnswersIn(
            application_id=conv.application_id, questionnaire_id=ver.questionnaire_id, questionnaire_version=ver.version,
            transcript_id=transcript_id, source="text", run_id=run_id,
        ),
    )  # fmt: skip
    req = set(required_ids(list(ver.questions)))
    settings = await settings_row(db)
    limit = settings.low_confidence_threshold
    confident = {a.question_id for a in out.answers if a.normalized is not None and a.confidence >= limit}
    return {
        "answered": [
            {
                "question_id": a.question_id,
                "normalized": a.normalized,
                "confidence": float(a.confidence),
                "flags": list(a.flags),
            }
            for a in out.answers
        ],
        "unanswered_required": [q for q in req if q not in confident],
    }


async def save_summary(
    ctx: RequestContext,
    db: AsyncSession,
    conv: ScreeningConversation,
    summary: str,
    gaps: list[str],
    suggested_next: str | None,
    final: bool,
    run_id: uuid.UUID | None,
) -> dict[str, Any]:
    _require_consent(conv)
    for text in (summary, *gaps):
        if SUMMARY_BLOCKLIST.search(text):
            raise invalid(
                "summary_language",
                "The summary may only state answers and gaps; it cannot describe the person",
            )
    if not final:
        conv.summary = summary[:3000]
        conv.gaps = gaps[:20]
        conv.suggested_next = suggested_next
        await db.flush()
        return {"status": conv.status}
    await conversations.complete(
        ctx, db, conv, summary=summary, gaps=gaps, suggested_next=suggested_next, run_id=run_id
    )
    return {"status": conv.status}


async def reminder(
    ctx: RequestContext, db: AsyncSession, conv: ScreeningConversation, run_id: uuid.UUID | None
) -> dict[str, Any]:
    from app.services.comms import channel_settings

    settings = await settings_row(db)
    if conv.reminders_sent >= settings.max_reminders:
        raise invalid("reminder_limit", "The reminder limit is reached")
    row = await channel_settings.get_row(db)
    if conv.channel == "whatsapp" and channel_settings.quiet_until(row.quiet_hours, now()) is not None:
        raise invalid("quiet_hours", "Reminders are not sent in quiet hours")
    name = await channels.owner_name(db, conv)
    title = await channels.demand_title(db, conv)
    body = f"A quick reminder: {name} is waiting for a few answers about the {title} role. Reply here whenever it suits you."
    turn = await turns.add_turn(
        db,
        conv,
        direction="outbound",
        author="agent",
        kind="reminder",
        body=body,
        template_key="screening_reminder",
        run_id=run_id,
    )
    if conv.channel == "whatsapp" and not wa_window_open(conv):
        sent = await channels.send_whatsapp_reminder(ctx, db, conv, turn)
    elif conv.channel == "portal_chat":
        channels._portal_notification(db, conv)
        sent = True
    elif conv.channel == "link_chat":
        sent = await channels.deliver(ctx, db, _as_email(conv), turn)
    else:
        sent = await channels.deliver(ctx, db, conv, turn)
    conv.reminders_sent += 1
    await db.flush()
    return {"sent": sent}


class _EmailView:
    """``link_chat`` reminders go out by email (the chat link is in the invitation)."""

    def __init__(self, conv: ScreeningConversation) -> None:
        self._conv = conv

    def __getattr__(self, name: str) -> Any:
        if name == "channel":
            return "email"
        return getattr(self._conv, name)


def _as_email(conv: ScreeningConversation) -> Any:
    return _EmailView(conv)


async def handoff(
    ctx: RequestContext,
    db: AsyncSession,
    conv: ScreeningConversation,
    reason: str,
    note: str | None,
    run_id: uuid.UUID | None,
) -> dict[str, Any]:
    task_id = await conversations.handoff(ctx, db, conv, reason, note, cancel=False, run_id=run_id)
    return {"task_id": task_id}


# ================================================================ candidate updates (high)
async def candidate_update_fields(db: AsyncSession, conv: ScreeningConversation) -> list[dict[str, Any]]:
    from app.models.pipeline import Screening
    from app.services.pipeline import screening as p1

    if conv.screening_id is None:
        return []
    row = await db.get(Screening, conv.screening_id)
    if row is None:
        return []
    return [p.model_dump(mode="json") for p in await p1.proposals(db, row)]


async def updates_version(db: AsyncSession, conv: ScreeningConversation) -> str:
    fields = await candidate_update_fields(db, conv)
    return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()[:32]


async def apply_candidate_updates(
    ctx: RequestContext, db: AsyncSession, conv: ScreeningConversation
) -> list[str]:
    from app.services.pipeline import screening as p1

    fields = [f["field"] for f in await candidate_update_fields(db, conv)]
    if not fields or conv.screening_id is None:
        raise invalid("no_proposal", "There are no candidate updates to apply")
    out = await p1.apply_candidate_updates(ctx, db, conv.screening_id, fields)
    return list(out.applied)


def expires_soon(conv: ScreeningConversation) -> bool:
    return conv.expires_at - now() < timedelta(hours=1)


__all__ = [
    "apply_candidate_updates",
    "ask",
    "candidate_update_fields",
    "clarify",
    "contact_channels",
    "extract",
    "handoff",
    "invite",
    "load",
    "postings",
    "questionnaire_view",
    "recipient_channel",
    "reminder",
    "reply_grounded",
    "save_summary",
    "updates_version",
]
