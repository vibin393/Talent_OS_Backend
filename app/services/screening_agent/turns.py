# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Conversation turns (Epic 57 A3): storage, transcript mirroring and the server-rendered texts.

Every turn is stored with a per-conversation ``seq`` (the conversation row is locked by the caller) and mirrored into
the conversation transcript (``transcripts_service.append_segments``) so ``extract_answers`` reads one source. Question
and clarification texts are rendered by the server from approved content (question ids / template keys); the model
never supplies them.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound
from app.models.screening_agent import MAX_TURN_BODY, ScreeningConversation, ScreeningTurn
from app.schemas.screening_agent import TurnOut
from app.schemas.transcripts import TranscriptSegmentIn
from app.services import entity_refs, transcripts_service
from app.services.screening_agent.common import now

SPEAKER = {"agent": "agent", "candidate": "candidate", "recruiter": "recruiter", "system": "agent"}
UNIT_HINTS = {
    "days": "in days", "weeks": "in weeks", "months": "in months", "years": "in years", "hours": "in hours",
    "km": "in km", "lpa": "in LPA", "percent": "as a percentage",
}  # fmt: skip


async def system_ctx(db: AsyncSession, conv: ScreeningConversation) -> RequestContext:
    """A context able to read the conversation's candidate (the Accountable Owner's principal, actor ``system``),
    used for server-side bookkeeping (transcripts) from candidate / link / task callers."""
    principal = await entity_refs.principal_for_user(db, conv.tenant_id, conv.owner_user_id)
    if principal is None:
        for uid in sorted(await entity_refs.role_user_ids(db, "tenant_admin")):
            principal = await entity_refs.principal_for_user(db, conv.tenant_id, uid)
            if principal is not None:
                break
    return RequestContext(tenant_id=conv.tenant_id, principal=principal, actor_kind="system")


async def ensure_transcript(db: AsyncSession, conv: ScreeningConversation) -> uuid.UUID:
    if conv.transcript_id is not None:
        return conv.transcript_id
    ctx = await system_ctx(db, conv)
    out = await transcripts_service.create(
        ctx, db, source_kind="screening_conversation", source_id=conv.id, candidate_id=conv.candidate_id,
        application_id=conv.application_id, language="en",
    )  # fmt: skip
    conv.transcript_id = out.id
    await db.flush()
    return out.id


async def next_seq(db: AsyncSession, conversation_id: uuid.UUID) -> int:
    current = (
        await db.execute(
            select(func.max(ScreeningTurn.seq)).where(ScreeningTurn.conversation_id == conversation_id)
        )
    ).scalar_one()
    return int(current or 0) + 1


async def add_turn(
    db: AsyncSession,
    conv: ScreeningConversation,
    *,
    direction: str,
    author: str,
    kind: str,
    body: str,
    channel: str | None = None,
    question_ids: Sequence[uuid.UUID] = (),
    citations: Sequence[str] = (),
    template_key: str | None = None,
    run_id: uuid.UUID | None = None,
    author_user_id: uuid.UUID | None = None,
) -> ScreeningTurn:
    """Store a turn and mirror it into the transcript. The caller holds the conversation row lock."""
    text = body.strip()[:MAX_TURN_BODY] or "(empty)"
    turn = ScreeningTurn(
        id=uuid.uuid4(), tenant_id=conv.tenant_id, conversation_id=conv.id, candidate_id=conv.candidate_id,
        seq=await next_seq(db, conv.id), direction=direction, author=author, kind=kind, channel=channel or conv.channel,
        body=text, question_ids=list(question_ids), citations=list(citations), template_key=template_key,
        run_id=run_id, author_user_id=author_user_id,
    )  # fmt: skip
    db.add(turn)
    await db.flush()
    transcript_id = await ensure_transcript(db, conv)
    ctx = await system_ctx(db, conv)
    try:
        async with db.begin_nested():
            out = await transcripts_service.append_segments(
                ctx, db, transcript_id,
                [TranscriptSegmentIn(speaker_role=SPEAKER.get(author, "unknown"), text=text)],
            )  # fmt: skip
            turn.transcript_seq = out.segment_count
    except Exception:
        turn.transcript_seq = None
    stamp = now()
    if direction == "inbound":
        conv.last_inbound_at = stamp
    else:
        conv.last_outbound_at = stamp
    await db.flush()
    return turn


async def list_turns(db: AsyncSession, conversation_id: uuid.UUID, after_seq: int = 0) -> list[ScreeningTurn]:
    return list(
        (
            await db.execute(
                select(ScreeningTurn)
                .where(ScreeningTurn.conversation_id == conversation_id, ScreeningTurn.seq > after_seq)
                .order_by(ScreeningTurn.seq)
            )
        ).scalars()
    )


def turn_out(row: ScreeningTurn) -> TurnOut:
    return TurnOut(
        id=row.id, seq=row.seq, direction=row.direction, author=row.author, kind=row.kind,
        channel=row.channel, body=row.body, question_ids=list(row.question_ids or []),
        citations=[str(c) for c in row.citations or []], template_key=row.template_key, created_at=row.created_at,
    )  # fmt: skip


async def last_turn(db: AsyncSession, conversation_id: uuid.UUID) -> ScreeningTurn | None:
    return (
        await db.execute(
            select(ScreeningTurn)
            .where(ScreeningTurn.conversation_id == conversation_id)
            .order_by(ScreeningTurn.seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


# ================================================================ server-rendered texts
def question_text(question: dict[str, Any]) -> str:
    label = str(question["label"]).strip()
    hint = ""
    if question.get("type") == "choice" and question.get("options"):
        hint = " Options: " + ", ".join(str(o) for o in question["options"]) + "."
    elif question.get("type") == "boolean":
        hint = " (yes / no)"
    elif question.get("type") == "date":
        hint = " (a date, e.g. 2026-11-01)"
    elif question.get("type") == "money":
        hint = f" (amount and currency{', ' + question['currency'] if question.get('currency') else ''})"
    elif question.get("type") == "rating":
        hint = " (1-5)"
    elif question.get("unit"):
        hint = f" ({UNIT_HINTS.get(str(question['unit']), question['unit'])})"
    return f"{label}{hint}"


def render_questions(questions: Sequence[dict[str, Any]]) -> str:
    if len(questions) == 1:
        return question_text(questions[0])
    return "\n".join(f"{i}. {question_text(q)}" for i, q in enumerate(questions, start=1))


def disclosure_text(recruiter: str, company: str, notice_url: str | None) -> str:
    notice = f" How we use AI: {notice_url}" if notice_url else ""
    return (
        f"Hello! I'm an AI assistant helping {recruiter} at {company} with a few screening questions about your "
        f'application. You can ask for a person at any time by replying "talk to a person". Before we start, do you '
        f"agree to chat with an AI assistant? Reply YES to agree or NO to decline.{notice}"
    )


def disclosure_text_chat(recruiter: str, company: str) -> str:
    return (
        f"Hello! I'm an AI assistant helping {recruiter} at {company} with a few screening questions about your "
        'application. You can ask for a person at any time. Please choose "I agree" to start or "No thanks" to decline.'
    )


async def get_turn(db: AsyncSession, turn_id: uuid.UUID) -> ScreeningTurn:
    row = await db.get(ScreeningTurn, turn_id)
    if row is None:
        raise NotFound("Turn not found")
    return row


__all__ = [
    "add_turn",
    "disclosure_text",
    "disclosure_text_chat",
    "ensure_transcript",
    "last_turn",
    "list_turns",
    "next_seq",
    "question_text",
    "render_questions",
    "system_ctx",
    "turn_out",
]
