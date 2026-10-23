# ruff: file-ignore[non-imperative-mood, line-too-long]
"""Recruitment Copilot facade (Epic 54, AD-64, FR-276..FR-280). The router and the ``copilot.*`` tasks call only this.

Turn orchestration lives in ``services/copilot`` (no SQL) and persistence in ``copilot_store``; this facade adds what
needs a session of its own or the AI gateway: the answer recorder (``on_run_finished``), the draft stream events, the
SSE wrapper with run-log polling, title generation, the purge job, settings and admin usage (metadata only).
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.agentic import copilot_capabilities
from app.core.agentic.copilot import enabled_capabilities
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import DomainError, Forbidden
from app.core.permissions import Principal
from app.schemas.agentic import AgentConfigIn
from app.schemas.copilot import (
    CopilotSettingsIn,
    CopilotSettingsOut,
    CopilotUsageOut,
    MessageIn,
    TurnOut,
    UsageCommentOut,
    UsageDayOut,
    UsageQuery,
)
from app.services import agent_runtime_service as runtime
from app.services import copilot_store as store
from app.services import entity_refs
from app.services.copilot import streaming, turns
from app.services.copilot.settings import DRAFT_EXPIRY_DEFAULT_HOURS, SETTINGS_FIELDS
from app.services.copilot.turns import (
    cancel_turn,
    close_conversation,
    create_conversation,
    delete_conversation,
    get_conversation,
    list_conversations,
    list_messages,
    rate_turn,
    rename_conversation,
    status,
)

FALLBACK_ANSWER = "I couldn't finish that answer."
SUMMARY_CAP = 2000
MAX_TOOLS = 30
EVENT_MESSAGES = {
    "draft_confirmed": "Done.",
    "draft_failed": "Couldn't complete the draft.",
    "draft_discarded": "Discarded.",
    "draft_expired": "This draft expired.",
    "draft_stale": "This draft is out of date. Ask Copilot to prepare it again.",
}


# ================================================================ turns (facade over services/copilot/turns)
async def _capability_text(ctx: Any, db: AsyncSession, principal: Principal) -> str | None:  # ruff: ignore[unused-function-argument]
    """Static prompt sections of the capabilities that are on (C-4), passed as trusted kickoff text: the runtime's
    system prompt is static per agent version, so capability sections ride with the turn (Known gap noted)."""
    from app.services import ai

    features = await ai.features_map(db)
    parts: list[str] = []
    for cap in enabled_capabilities(features, principal.has):
        if not cap.prompt_section:
            continue
        try:
            prompt = ai.load_prompt(cap.prompt_section.removesuffix(".md"))
        except (FileNotFoundError, ValueError):
            continue
        parts.append(f"[{cap.title}]\n{prompt.system}")
    return "\n\n".join(parts)[:12_000] or None


async def post_message(
    ctx: RequestContext, db: AsyncSession, conversation_id: uuid.UUID, data: MessageIn
) -> TurnOut:
    principal = ctx.principal
    name = ""
    today = None
    if isinstance(principal, Principal) and ctx.tenant_id is not None:
        info = (await entity_refs.users(db, [principal.id])).get(principal.id)
        name = info.name if info is not None else ""
        tenant = await entity_refs.tenant(db, ctx.tenant_id)
        today = datetime.now(tenant.tz).date().isoformat()
    if isinstance(principal, Principal) and principal.has(turns.PERMISSION):
        await ensure_draft_expiry(ctx, db)
    return await turns.post_message(
        ctx,
        db,
        conversation_id,
        data,
        user_display_name=name,
        today=today,
        capability_text=_capability_text,
        on_segment=lambda cid: _enqueue_title(ctx.tenant_id, cid, segment=True),
    )


def _enqueue_title(tenant_id: uuid.UUID | None, conversation_id: uuid.UUID, *, segment: bool) -> None:
    from app.tasks import copilot as copilot_tasks
    from app.tasks.runtime import enqueue

    if tenant_id is not None:
        enqueue(copilot_tasks.generate_title, str(tenant_id), str(conversation_id), segment)


# ================================================================ answers and stream events
def _owner_ctx(
    tenant_id: uuid.UUID, user_id: uuid.UUID, principal: Principal | None = None
) -> RequestContext:
    return RequestContext(
        tenant_id=tenant_id, principal=principal or Principal(type="user", id=user_id, tenant_id=tenant_id)
    )


async def _owner_principal(db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> Principal:
    live = await entity_refs.principal_for_user(db, tenant_id, user_id)
    return live if live is not None else Principal(type="user", id=user_id, tenant_id=tenant_id)


def _tool_entry(call: Any) -> dict[str, Any]:
    try:
        title = get_agent_tool_title(call.tool_key)
    except KeyError:
        title = call.tool_key
    return {
        "tool_key": call.tool_key, "title": title, "outcome": call.outcome,
        "summary": (call.result_summary or "")[:200], "error_code": call.error_code,
    }  # fmt: skip


def get_agent_tool_title(tool_key: str) -> str:
    from app.core.agentic import get_tool

    return get_tool(tool_key).title


async def publish_run_artifacts(run: Any) -> None:
    """``tool.finished`` per logged tool call and ``draft.created`` per draft, each once (Known gap 2 path)."""
    for call in run.tool_calls:
        entry = _tool_entry(call)
        await streaming.publish(
            run.id,
            "tool.finished",
            {k: entry[k] for k in ("tool_key", "title", "outcome", "summary")},
            once=f"tool:{call.id}",
        )


async def _publish_draft(run_id: uuid.UUID, proposal: Any) -> None:
    await streaming.publish(
        run_id, "draft.created",
        {"proposal_id": str(proposal.id), "tool_key": proposal.tool_key, "title": proposal.title, "preview": proposal.preview},
        once=f"draft:{proposal.id}",
    )  # fmt: skip


async def _proposals(ctx: RequestContext, db: AsyncSession, run: Any) -> list[Any]:
    out = []
    for summary in run.proposals:
        try:
            out.append(await runtime.get_proposal(ctx, db, summary.id))
        except DomainError:
            continue
    return out


async def _answer_text(run: Any) -> str:
    summary = str((run.result or {}).get("summary") or "").strip()
    streamed = (await streaming.delta_text(run.id)).strip()
    if summary and len(summary) >= SUMMARY_CAP and streamed:
        anchor = streamed.find(summary[:200])
        if anchor >= 0:
            return streamed[anchor:][:20_000]
    if summary:
        return summary
    if streamed:
        return streamed[:20_000]
    return FALLBACK_ANSWER


async def record_answer(tenant_id: uuid.UUID, run_id: uuid.UUID) -> dict[str, Any] | None:
    """Store the assistant message of a finished Copilot turn, then publish the tool / draft events and
    ``turn.completed`` / ``turn.failed``. Idempotent per run."""
    async with tenant_session(tenant_id) as db:
        found = await store.messages.run_owner(db, run_id)
        if found is None:
            return None
        conv, _user_msg = found
        existing = await store.messages.turn_message(db, run_id, "assistant")
        principal = await _owner_principal(db, tenant_id, conv.user_id)
        run = await runtime.get_run(_owner_ctx(tenant_id, conv.user_id, principal), db, run_id)
        if run.status in ("queued", "running", "waiting"):
            return None
        if existing is None:
            settings = await store.settings_row.get(db, tenant_id)
            refs = store.runs_bridge.refs_of(run)
            cards = [
                {"kind": c.target_type, "id": str(c.target_id)}
                for c in run.tool_calls
                if c.outcome == "executed" and c.risk == "read" and c.target_type and c.target_id
            ][:20]
            content = {
                "text": await _answer_text(run) if run.status == "completed" else str((run.result or {}).get("summary") or ""),
                "status": run.status, "reason_code": run.reason_code, "refs": refs,
                "tools": [_tool_entry(c) for c in run.tool_calls][:MAX_TOOLS],
                "drafts": [str(p.id) for p in run.proposals], "cards": cards,
            }  # fmt: skip
            earlier = [m for m in await store.messages.all_of(db, conv.id) if m.role == "assistant"]
            seq = store.conversations.touch(conv, retention_days=settings.retention_days)
            existing = await store.messages.add(
                db, conv, seq=seq, role="assistant", content=content, run_id=run_id,
                candidate_ids=store.runs_bridge.candidate_ids_of(run),
            )  # fmt: skip
            first_answer = not earlier
        else:
            first_answer = False
        seq_out, conv_id, title_locked = existing.seq, conv.id, conv.title_locked
        tool_runs = run
        drafts = await _proposals(_owner_ctx(tenant_id, conv.user_id, principal), db, run)
    await publish_run_artifacts(tool_runs)
    for p in drafts:
        await _publish_draft(run_id, p)
    final = "turn.completed" if tool_runs.status in ("completed", "cancelled") else "turn.failed"
    await streaming.publish(
        run_id, final,
        {"run_id": str(run_id), "status": tool_runs.status, "reason_code": tool_runs.reason_code, "message_seq": seq_out},
        once="final",
    )  # fmt: skip
    return {"conversation_id": conv_id, "first_answer": first_answer, "title_locked": title_locked}


async def on_draft_created(tenant_id: uuid.UUID, run_id: uuid.UUID, proposal_id: uuid.UUID) -> None:
    async with tenant_session(tenant_id) as db:
        found = await store.messages.run_owner(db, run_id)
        if found is None:
            return
        conv = found[0]
        try:
            proposal = await runtime.get_proposal(_owner_ctx(tenant_id, conv.user_id), db, proposal_id)
        except DomainError:
            return
    await _publish_draft(run_id, proposal)


DECIDED_KIND = {
    "executed": "draft_confirmed", "execution_failed": "draft_failed", "discarded": "draft_discarded",
    "expired": "draft_expired", "stale": "draft_stale",
}  # fmt: skip


async def on_draft_decided(tenant_id: uuid.UUID, run_id: uuid.UUID, proposal_id: uuid.UUID) -> None:
    async with tenant_session(tenant_id) as db:
        found = await store.messages.run_owner(db, run_id)
        if found is None:
            return
        conv = found[0]
        try:
            proposal = await runtime.get_proposal(_owner_ctx(tenant_id, conv.user_id), db, proposal_id)
        except DomainError:
            return
        kind = DECIDED_KIND.get(proposal.status)
        if kind is None or proposal.origin != "copilot":
            return
        message = EVENT_MESSAGES[kind]
        if kind == "draft_failed" and proposal.error_code:
            message = f"Couldn't complete: {proposal.error_code}"
        settings = await store.settings_row.get(db, tenant_id)
        seq = store.conversations.touch(conv, retention_days=settings.retention_days)
        await store.messages.add(
            db, conv, seq=seq, role="event",
            content={"kind": kind, "proposal_id": str(proposal_id), "error_code": proposal.error_code, "message": message},
        )  # fmt: skip


async def poll_run(tenant_id: uuid.UUID, user_id: uuid.UUID, run_id: uuid.UUID) -> None:
    """While the SSE is open: derive ``tool.finished`` / ``draft.created`` from the run log (Known gap 2)."""
    async with tenant_session(tenant_id) as db:
        principal = Principal(type="user", id=user_id, tenant_id=tenant_id)
        ctx = _owner_ctx(tenant_id, user_id, principal)
        run = await runtime.get_run(ctx, db, run_id)
        drafts = await _proposals(ctx, db, run)
    await publish_run_artifacts(run)
    for p in drafts:
        await _publish_draft(run_id, p)


async def stream_events(
    ctx: RequestContext, db: AsyncSession, run_id: uuid.UUID, *, last_event_id: str | None
) -> AsyncIterator[bytes]:
    """Owner check now (404 for anyone else), then the SSE generator (runs after the request session closed)."""
    conv, _msg = await turns.owned_turn(ctx, db, run_id)
    tenant_id, user_id = conv.tenant_id, conv.user_id

    async def poll() -> None:
        await poll_run(tenant_id, user_id, run_id)

    return streaming.sse(run_id, last_event_id=last_event_id, poll=poll)


# ================================================================ titles
class TitleOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=60)
    summary: str = Field(default="", max_length=1500)


async def generate_title(tenant_id: uuid.UUID, conversation_id: uuid.UUID, *, segment: bool = False) -> bool:
    """``copilot_title`` (fast tier, feature ``agent_copilot``): set the title unless locked, and the segment summary.
    On failure the provisional title stays."""
    from app.services import ai

    async with tenant_session(tenant_id) as db:
        conv = await store.conversations.get(db, conversation_id)
        if conv is None:
            return False
        rows = await store.messages.list_after(db, conv.id, limit=8 if not segment else 200)
        lines = [
            f"{'User' if m.role == 'user' else 'Copilot'}: {str((m.content or {}).get('text') or '')[:2000]}"
            for m in rows
            if m.role in ("user", "assistant") and not (m.content or {}).get("redacted")
        ][: 8 if not segment else 200]
        ctx = RequestContext(
            tenant_id=tenant_id, principal=Principal(type="user", id=conv.user_id, tenant_id=tenant_id)
        )
        result = await ai.run(
            ctx, db, "agent_copilot", "copilot_title", {}, {"conversation": "\n".join(lines)[:20_000]}, TitleOut,
            model_tier="fast",
        )  # fmt: skip
        if not result.ok or not isinstance(result.data, TitleOut):
            return False
        if not conv.title_locked and result.data.title.strip():
            conv.title = result.data.title.strip()[:120]
        if segment and result.data.summary:
            conv.context_summary = result.data.summary[:4000]
        return True


# ================================================================ purge
async def purge_expired(tenant_id: uuid.UUID) -> int:
    async with tenant_session(tenant_id) as db:
        count = await store.purge.purge_expired(db)
        if count:
            audit.record("copilot.conversation_purged", "copilot_conversation", None, session=db, count=count)
        return count


# ================================================================ settings / usage
def _require(ctx: RequestContext, *keys: str) -> Principal:
    principal = ctx.principal
    if not isinstance(principal, Principal) or not any(principal.has(k) for k in keys):
        raise Forbidden("You do not have permission to manage Copilot")
    return principal


async def get_settings(ctx: RequestContext, db: AsyncSession) -> CopilotSettingsOut:
    _require(ctx, "ai:govern", "ai:agent_configure")
    assert ctx.tenant_id is not None
    row = await store.settings_row.get(db, ctx.tenant_id)
    avail = await store.settings_row.availability(ctx, db, ctx.tenant_id)
    catalog = next((a for a in await runtime.get_agents(ctx, db) if a.key == "copilot"), None)
    expiry = DRAFT_EXPIRY_DEFAULT_HOURS
    config_hours = await _draft_expiry(db)
    if config_hours is not None:
        expiry = config_hours
    return CopilotSettingsOut(
        retention_days=row.retention_days, questions_per_hour=row.questions_per_hour,
        max_open_conversations=row.max_open_conversations, allow_feedback_comments=row.allow_feedback_comments,
        draft_expiry_hours=expiry, enabled=bool(catalog.enabled) if catalog else False,
        agentic_enabled=bool(avail.features.get("agentic")), updated_at=row.updated_at,
    )  # fmt: skip


async def _draft_expiry(db: AsyncSession) -> int | None:
    from sqlalchemy import select

    from app.models.agent_runtime import AgentConfig

    return (
        await db.execute(select(AgentConfig.proposal_expiry_hours).where(AgentConfig.agent_key == "copilot"))
    ).scalar_one_or_none()


async def ensure_draft_expiry(ctx: RequestContext, db: AsyncSession) -> None:
    """Default Copilot drafts to 24 h (FR-278) unless the tenant set another value."""
    if await _draft_expiry(db) is None:
        from sqlalchemy.exc import IntegrityError

        try:
            async with (
                db.begin_nested()
            ):  # concurrent first turns race on the config row: one wins, all get 24 h
                await runtime.update_config(
                    ctx, db, "copilot", AgentConfigIn(proposal_expiry_hours=DRAFT_EXPIRY_DEFAULT_HOURS)
                )
        except IntegrityError:
            return


async def update_settings(
    ctx: RequestContext, db: AsyncSession, data: CopilotSettingsIn
) -> CopilotSettingsOut:
    principal = _require(ctx, "ai:agent_configure")
    assert ctx.tenant_id is not None
    changes = {
        k: getattr(data, k)
        for k in SETTINGS_FIELDS
        if k in data.model_fields_set and getattr(data, k) is not None
    }
    if changes:
        await store.settings_row.update(db, ctx.tenant_id, changes, principal.id)
    if data.draft_expiry_hours is not None:
        await runtime.update_config(
            ctx, db, "copilot", AgentConfigIn(proposal_expiry_hours=data.draft_expiry_hours)
        )
        audit.record(
            "copilot.settings_changed", "agent_config", None, session=db, fields=["draft_expiry_hours"]
        )
    else:
        await ensure_draft_expiry(ctx, db)
    return await get_settings(ctx, db)


async def admin_usage(ctx: RequestContext, db: AsyncSession, q: UsageQuery) -> CopilotUsageOut:
    _require(ctx, "ai:govern")
    today = datetime.now(UTC).date()
    date_to = q.date_to or today
    date_from = q.date_from or (date_to - timedelta(days=29))
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    date_from = max(date_from, date_to - timedelta(days=366))
    data = await store.usage.daily(db, date_from, date_to)
    return CopilotUsageOut(
        date_from=date_from, date_to=date_to,
        days=[UsageDayOut(**d) for d in data["days"]],
        comments=[UsageCommentOut(**c) for c in data["comments"]],
    )  # fmt: skip


def capability_keys() -> list[str]:
    return sorted(copilot_capabilities())


__all__ = [
    "admin_usage",
    "cancel_turn",
    "capability_keys",
    "close_conversation",
    "create_conversation",
    "delete_conversation",
    "ensure_draft_expiry",
    "generate_title",
    "get_conversation",
    "get_settings",
    "list_conversations",
    "list_messages",
    "on_draft_created",
    "on_draft_decided",
    "poll_run",
    "post_message",
    "purge_expired",
    "rate_turn",
    "record_answer",
    "rename_conversation",
    "status",
    "stream_events",
    "update_settings",
]
