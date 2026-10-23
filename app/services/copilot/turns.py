# ruff: file-ignore[line-too-long]
"""Copilot conversations and turns (Epic 54 A5, AD-64). No SQL here (contract ``p4-agents-isolation``): persistence is
``copilot_store``, runs go through ``agent_runtime_service``. Every function takes the request context and session.

* Privacy: a conversation, its messages, its turn stream and its drafts are visible to their user only; anyone else
  (an administrator included) gets 404 (coordinator decision 6).
* ``post_message``: permission -> owner -> open -> available -> rate limit -> store the question -> ``start_run``
  (one run per question on queue ``conversations``; owner = the user; ``dedup_key = copilot:<conversation>:<seq>``)
  -> ``turn.started`` on the stream -> ``TurnOut``.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from app.core import audit
from app.core.errors import ConflictError, Forbidden, NotFound, RateLimited, ValidationFailed
from app.core.pagination import Page
from app.core.permissions import Principal
from app.schemas.copilot import (
    ConversationCreate,
    ConversationOut,
    ConversationQuery,
    ConversationSummaryOut,
    CopilotStatusOut,
    FeedbackIn,
    FeedbackOut,
    MessageIn,
    MessageOut,
    ModeOut,
    TurnOut,
)
from app.services import agent_runtime_service as runtime
from app.services import copilot_store as store
from app.services.copilot import capabilities, ratelimit, streaming
from app.services.copilot.context import build_kickoff, page_candidate_ids
from app.services.copilot.settings import unavailable_reason

PERMISSION = "copilot:use"
STREAM_URL = "/api/v1/copilot/turns/{run_id}/events"
PROVISIONAL_TITLE_CHARS = 60
UNAVAILABLE_TEXT = {
    "kill_switch": "AI agents are paused by your administrator. Copilot is unavailable for now.",
    "feature_off": "Copilot is turned off for your organisation.",
    "agent_not_available": "Copilot is turned off for your organisation.",
    "owner_unavailable": "Copilot couldn't start for your account.",
}

# Hook for the facade: extra trusted kickoff text of the enabled capabilities (their static prompt sections).
CapabilityText = Callable[[Any, Any, Principal], Awaitable[str | None]]


def _principal(ctx: Any) -> Principal:
    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.type != "user":
        raise Forbidden("Copilot is available to signed-in users only")
    return principal


def _require_use(ctx: Any) -> Principal:
    principal = _principal(ctx)
    if not principal.has(PERMISSION):
        raise Forbidden("You do not have access to Copilot")
    return principal


def _now() -> datetime:
    return datetime.now(UTC)


# ================================================================ DTO helpers
def message_out(m: Any) -> MessageOut:
    return MessageOut(
        id=m.id, seq=m.seq, role=m.role, content=dict(m.content or {}), run_id=m.run_id,
        redacted=m.redacted_at is not None or bool((m.content or {}).get("redacted")), created_at=m.created_at,
    )  # fmt: skip


def conversation_summary(c: Any) -> ConversationSummaryOut:
    return ConversationSummaryOut(
        id=c.id, agent_key=c.agent_key, mode_label=capabilities.mode_label(c.agent_key), title=c.title, status=c.status,
        message_count=c.message_count, last_message_at=c.last_message_at, created_at=c.created_at, expires_at=c.expires_at,
    )  # fmt: skip


def conversation_out(c: Any) -> ConversationOut:
    return ConversationOut(**conversation_summary(c).model_dump(), context_ref=c.context_ref)


# ================================================================ status
async def _mode_on(features: dict[str, bool], agent_key: str) -> bool:
    return bool(features.get("agentic")) and bool(features.get(f"agent_{agent_key}"))


async def status(ctx: Any, db: Any) -> CopilotStatusOut:
    principal = _principal(ctx)
    tenant_id = ctx.tenant_id
    settings = await store.settings_row.get(db, tenant_id)
    avail = await store.settings_row.availability(ctx, db, tenant_id)
    reason = unavailable_reason(
        has_permission=principal.has(PERMISSION), features=avail.features, tenant_kill=avail.tenant_kill,
        global_kill=avail.global_kill, budget_exhausted=avail.budget_exhausted,
    )  # fmt: skip
    modes = [ModeOut(agent_key="copilot", label=capabilities.mode_label("copilot"))]
    if principal.has(PERMISSION):
        modes += [
            ModeOut(agent_key=spec.key, label=capabilities.mode_label(spec.key))
            for spec in capabilities.mode_specs()
            if await _mode_on(avail.features, spec.key)
        ]
    left = (
        await ratelimit.remaining(tenant_id, principal.id, settings.questions_per_hour)
        if reason is None
        else 0
    )
    return CopilotStatusOut(
        available=reason is None, reason=reason, questions_left_this_hour=left,
        questions_per_hour=settings.questions_per_hour, retention_days=settings.retention_days,
        allow_feedback_comments=settings.allow_feedback_comments, modes=modes,
    )  # fmt: skip


# ================================================================ conversations
async def list_conversations(ctx: Any, db: Any, q: ConversationQuery) -> Page[ConversationSummaryOut]:
    principal = _require_use(ctx)
    page = await store.conversations.list_owned(
        db, principal.id, status=q.status, page=q.page, page_size=q.page_size
    )
    return Page[ConversationSummaryOut](
        items=[conversation_summary(c) for c in page.items],
        total=page.total,
        page=page.page,
        page_size=page.page_size,
    )


async def _check_mode(ctx: Any, db: Any, agent_key: str) -> None:
    if agent_key == "copilot":
        return
    if agent_key not in {s.key for s in capabilities.mode_specs()}:
        raise ValidationFailed("Unknown Copilot mode", code="unknown_mode")
    avail = await store.settings_row.availability(ctx, db, ctx.tenant_id)
    if not await _mode_on(avail.features, agent_key):
        raise ValidationFailed("This Copilot mode is not turned on", code="unknown_mode")


async def create_conversation(ctx: Any, db: Any, data: ConversationCreate) -> ConversationOut:
    principal = _require_use(ctx)
    await _check_mode(ctx, db, data.agent_key)
    settings = await store.settings_row.get(db, ctx.tenant_id)
    if await store.conversations.count_open(db, principal.id) >= settings.max_open_conversations:
        raise ConflictError(
            "You have too many open conversations. Close or delete some first.",
            code="too_many_open_conversations",
            details={"max_open_conversations": settings.max_open_conversations},
        )
    conv = await store.conversations.create(
        db, tenant_id=ctx.tenant_id, user_id=principal.id, agent_key=data.agent_key,
        context_ref=data.page_context.model_dump(mode="json") if data.page_context else None,
        retention_days=settings.retention_days,
    )  # fmt: skip
    audit.record(
        "copilot.conversation_created", "copilot_conversation", conv.id, session=db, agent_key=conv.agent_key
    )
    return conversation_out(conv)


async def get_conversation(ctx: Any, db: Any, conversation_id: uuid.UUID) -> ConversationOut:
    principal = _principal(ctx)
    return conversation_out(await store.conversations.owned(db, conversation_id, principal.id))


async def rename_conversation(ctx: Any, db: Any, conversation_id: uuid.UUID, title: str) -> ConversationOut:
    principal = _principal(ctx)
    conv = await store.conversations.owned(db, conversation_id, principal.id, lock=True)
    conv.title = title[:120]
    conv.title_locked = True
    await db.flush()
    return conversation_out(conv)


async def close_conversation(ctx: Any, db: Any, conversation_id: uuid.UUID) -> ConversationOut:
    principal = _principal(ctx)
    conv = await store.conversations.owned(db, conversation_id, principal.id, lock=True)
    if conv.status != "closed":
        await store.purge.discard_conversation_drafts(db, conv)
        store.conversations.close(conv)
        await db.flush()
    return conversation_out(conv)


async def delete_conversation(ctx: Any, db: Any, conversation_id: uuid.UUID) -> None:
    principal = _principal(ctx)
    conv = await store.conversations.owned(db, conversation_id, principal.id, lock=True)
    counts = await store.purge.remove(db, [conv])
    audit.record(
        "copilot.conversation_deleted", "copilot_conversation", conversation_id, session=db,
        runs=counts["runs"], drafts=counts["drafts"],
    )  # fmt: skip


async def list_messages(
    ctx: Any, db: Any, conversation_id: uuid.UUID, *, after_seq: int = 0, limit: int = 100
) -> list[MessageOut]:
    principal = _principal(ctx)
    conv = await store.conversations.owned(db, conversation_id, principal.id)
    rows = await store.messages.list_after(
        db, conv.id, after_seq=max(0, after_seq), limit=min(max(limit, 1), 200)
    )
    return [message_out(m) for m in rows]


# ================================================================ turns
def _today() -> str:
    return _now().date().isoformat()


async def post_message(
    ctx: Any,
    db: Any,
    conversation_id: uuid.UUID,
    data: MessageIn,
    *,
    user_display_name: str = "",
    today: str | None = None,
    capability_text: CapabilityText | None = None,
    on_segment: Callable[[uuid.UUID], None] | None = None,
) -> TurnOut:
    principal = _require_use(ctx)
    conv = await store.conversations.owned(db, conversation_id, principal.id, lock=True)
    if conv.status != "open":
        raise ConflictError("This conversation is closed. Start a new chat.", code="conversation_closed")
    state = await status(ctx, db)
    if not state.available:
        raise ConflictError(
            "Copilot is not available right now", code="copilot_unavailable", details={"reason": state.reason}
        )
    if conv.agent_key != "copilot":
        await _check_mode(ctx, db, conv.agent_key)
    settings = await store.settings_row.get(db, ctx.tenant_id)
    window = await ratelimit.hit(ctx.tenant_id, principal.id, settings.questions_per_hour)
    if not window.allowed:
        audit.record("copilot.turn_rate_limited", "copilot_conversation", conv.id, session=db)
        await db.commit()  # keep the audit row (the 429 rolls the request back otherwise)
        minutes = max(1, (window.retry_after_s + 59) // 60)
        exc = RateLimited(
            f"You've reached the limit of {settings.questions_per_hour} questions an hour. Try again in {minutes} minutes.",
            code="copilot_rate_limited",
            details={"retry_after_s": window.retry_after_s, "limit": settings.questions_per_hour},
        )
        exc.response_hooks.append(lambda r: r.headers.__setitem__("Retry-After", str(window.retry_after_s)))
        raise exc
    page_ctx = data.page_context.model_dump(mode="json") if data.page_context else conv.context_ref
    prior = [(m.role, dict(m.content or {})) for m in await store.messages.all_of(db, conv.id)]
    parent = await store.messages.last_run_id(db, conv.id)
    seq = store.conversations.touch(conv, retention_days=settings.retention_days)
    if seq == 1:
        conv.context_ref = page_ctx
        if not conv.title_locked and not conv.title:
            conv.title = data.text[:PROVISIONAL_TITLE_CHARS]
    user_msg = await store.messages.add(
        db, conv, seq=seq, role="user", content={"text": data.text, "page_context": page_ctx},
        candidate_ids=page_candidate_ids(page_ctx),
    )  # fmt: skip
    extra = (
        await capability_text(ctx, db, principal)
        if capability_text is not None and conv.agent_key == "copilot"
        else None
    )
    kickoff, cut = build_kickoff(
        question=data.text, page_context=page_ctx, today=today or _today(),
        user_display_name=user_display_name or "the user", prior=prior, summary=conv.context_summary,
        capability_instructions=extra,
    )  # fmt: skip
    ref = await runtime.start_run(
        ctx, db, agent_key=conv.agent_key, trigger_kind="copilot", origin="copilot", owner_user_id=principal.id,
        conversation_id=conv.id, parent_run_id=parent, dedup_key=f"copilot:{conv.id}:{seq}", kickoff=kickoff,
    )  # fmt: skip
    if ref.run_id is None or ref.status in ("not_started", "skipped"):
        reason = ref.reason or "not_started"
        text = UNAVAILABLE_TEXT.get(reason, "Copilot couldn't start this answer. Try again.")
        seq2 = store.conversations.touch(conv, retention_days=settings.retention_days)
        await store.messages.add(
            db, conv, seq=seq2, role="assistant",
            content={"text": text, "status": "unavailable", "reason_code": reason, "refs": [], "tools": [], "drafts": [], "cards": []},
        )  # fmt: skip
        return TurnOut(
            message=message_out(user_msg), run_id=None, stream_url=None, status="unavailable", reason=reason
        )
    user_msg.run_id = ref.run_id
    await db.flush()
    if cut and on_segment is not None:
        on_segment(conv.id)  # refresh the segment summary (``copilot_title``) for the next turns
    await streaming.publish(
        ref.run_id,
        "turn.started",
        {"run_id": str(ref.run_id), "conversation_id": str(conv.id), "seq": seq},
        once="started",
    )
    return TurnOut(
        message=message_out(user_msg), run_id=ref.run_id, stream_url=STREAM_URL.format(run_id=ref.run_id),
        status=ref.status, reason=ref.reason,
    )  # fmt: skip


async def owned_turn(ctx: Any, db: Any, run_id: uuid.UUID) -> tuple[Any, Any]:
    """(conversation, user message) of a turn run owned by the caller; 404 for anyone else."""
    principal = _principal(ctx)
    found = await store.messages.run_owner(db, run_id)
    if found is None or found[0].user_id != principal.id:
        raise NotFound("Turn not found")
    return found


async def cancel_turn(ctx: Any, db: Any, run_id: uuid.UUID) -> TurnOut:
    _conv, msg = await owned_turn(ctx, db, run_id)
    ref = await runtime.cancel_run(ctx, db, run_id, reason="Stopped by the user in Copilot")
    return TurnOut(
        message=message_out(msg), run_id=run_id, stream_url=None, status=ref.status, reason=ref.reason
    )


async def rate_turn(ctx: Any, db: Any, run_id: uuid.UUID, data: FeedbackIn) -> FeedbackOut:
    principal = _principal(ctx)
    conv, _msg = await owned_turn(ctx, db, run_id)
    settings = await store.settings_row.get(db, ctx.tenant_id)
    comment = (data.comment or "").strip() or None
    if comment and not settings.allow_feedback_comments:
        raise ValidationFailed("Feedback comments are turned off", code="comments_disabled")
    row = await store.feedback.upsert(
        db, tenant_id=ctx.tenant_id, run_id=run_id, conversation_id=conv.id, user_id=principal.id,
        agent_key=conv.agent_key, rating=data.rating, comment=comment,
    )  # fmt: skip
    return FeedbackOut(run_id=row.run_id, rating=row.rating, comment=row.comment, updated_at=row.updated_at)  # type: ignore[arg-type]


__all__ = [
    "PERMISSION",
    "cancel_turn",
    "close_conversation",
    "conversation_out",
    "conversation_summary",
    "create_conversation",
    "delete_conversation",
    "get_conversation",
    "list_conversations",
    "list_messages",
    "message_out",
    "owned_turn",
    "post_message",
    "rate_turn",
    "rename_conversation",
    "status",
]
