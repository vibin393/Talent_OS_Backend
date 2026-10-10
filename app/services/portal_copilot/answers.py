# ruff: noqa: E501, D401
"""Run completion of portal Copilot turns (Epic 77 A4). Subscribed to the P4 ``AgentRunFinished`` / ``AgentRunFailed``
events filtered by ``agent_key in {copilot_client, copilot_vendor, copilot_candidate}`` (the P4 Copilot handler only
takes origin ``copilot`` agents, so the two never overlap). Seals and stores the assistant message (text, links,
tools, draft ids) and publishes ``tool.finished`` / ``draft.created`` / ``turn.completed`` | ``turn.failed`` through
the reused P4 ``copilot.streaming``. Idempotent per run. Reads the run log directly (portal principals are not
internal run viewers); never logs content."""

import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import tenant_session
from app.models.agent_runtime import ACTIVE_RUN_STATUSES, AgentProposal, AgentRun, AgentToolCall
from app.services.copilot import streaming
from app.services.portal_copilot import settings as settings_mod
from app.services.portal_copilot import store

PORTAL_AGENT_KEYS = frozenset({"copilot_client", "copilot_vendor", "copilot_candidate"})
FALLBACK_ANSWER = "I couldn't finish that answer."
FAILED_ANSWER = "Something went wrong and I couldn't answer. Try again."
ACCESS_CHANGED = "Your access changed. Refresh the page."
_LINK_RE = re.compile(r"\]\((/(?:client|vendor|portal|careers)[^)\s]*)\)")


def _title(tool_key: str) -> str:
    from app.core.agentic import get_tool

    try:
        return get_tool(tool_key).title
    except KeyError:
        return tool_key


async def _calls(db: AsyncSession, run_id: uuid.UUID) -> list[AgentToolCall]:
    return list(
        (
            await db.execute(
                select(AgentToolCall)
                .where(AgentToolCall.run_id == run_id)
                .order_by(AgentToolCall.step_no, AgentToolCall.created_at)
            )
        ).scalars()
    )


async def drafts_of(db: AsyncSession, run_id: uuid.UUID) -> list[AgentProposal]:
    return list(
        (
            await db.execute(
                select(AgentProposal)
                .where(AgentProposal.run_id == run_id, AgentProposal.origin == "portal")
                .order_by(AgentProposal.created_at)
            )
        ).scalars()
    )


def editable_fields(args: dict[str, Any]) -> list[str]:
    """Whitelisted edit fields of a portal Draft: every argument except the record it targets (``*_id``)."""
    return sorted(k for k in args if not k.endswith("_id"))


def draft_json(p: AgentProposal) -> dict[str, Any]:
    return {
        "proposal_id": str(p.id), "tool_key": p.tool_key, "title": p.title, "preview": p.preview, "status": p.status,
        "expires_at": p.expires_at.isoformat() if p.expires_at else None,
        "editable_fields": editable_fields(p.args or {}),
    }  # fmt: skip


async def _answer_text(run: AgentRun) -> str:
    summary = str((run.result or {}).get("summary") or "").strip()
    if summary:
        return summary[:20_000]
    streamed = (await streaming.delta_text(run.id)).strip()
    return streamed[:20_000] if streamed else FALLBACK_ANSWER


async def publish_artifacts(
    run_id: uuid.UUID, calls: list[AgentToolCall], drafts: list[AgentProposal]
) -> None:
    for c in calls:
        await streaming.publish(
            run_id,
            "tool.finished",
            {"tool_key": c.tool_key, "title": _title(c.tool_key), "outcome": c.outcome},
            once=f"tool:{c.id}",
        )
    for p in drafts:
        await streaming.publish(run_id, "draft.created", draft_json(p), once=f"draft:{p.id}")


async def record_answer(tenant_id: uuid.UUID, run_id: uuid.UUID) -> bool:
    async with tenant_session(tenant_id) as db:
        found = await store.turn_of_run(db, run_id)
        run = await db.get(AgentRun, run_id)
        if (
            found is None
            or run is None
            or run.agent_key not in PORTAL_AGENT_KEYS
            or run.status in ACTIVE_RUN_STATUSES
        ):
            return False
        conv, _user = found
        calls = await _calls(db, run_id)
        drafts = await drafts_of(db, run_id)
        existing = await store.answer_of_run(db, run_id)
        if existing is None:
            if run.status == "completed":
                text = await _answer_text(run)
            elif run.reason_code in ("owner_unavailable", "principal_inactive"):
                text = ACCESS_CHANGED
            else:
                text = FAILED_ANSWER
            content = {
                "text": text, "status": run.status, "reason_code": run.reason_code,
                "links": sorted(set(_LINK_RE.findall(text)))[:20],
                "tools": [{"tool_key": c.tool_key, "title": _title(c.tool_key), "outcome": c.outcome} for c in calls][:30],
                "drafts": [draft_json(p) for p in drafts],
            }  # fmt: skip
            s = await settings_mod.get(tenant_id, db)
            seq = store.touch(conv, retention_days=s.retention_days)
            existing = await store.add_message(
                db, conv, seq=seq, role="assistant", content=content, run_id=run_id
            )
        seq_out, status, reason = existing.seq, run.status, run.reason_code
    await publish_artifacts(run_id, calls, drafts)
    final = "turn.completed" if status in ("completed", "cancelled") else "turn.failed"
    await streaming.publish(
        run_id,
        final,
        {"run_id": str(run_id), "status": status, "reason_code": reason, "message_seq": seq_out},
        once="final",
    )
    return True


async def poll(tenant_id: uuid.UUID, run_id: uuid.UUID) -> None:
    """While the SSE is open: derive ``tool.finished`` / ``draft.created`` from the run log."""
    async with tenant_session(tenant_id) as db:
        calls = await _calls(db, run_id)
        drafts = await drafts_of(db, run_id)
    await publish_artifacts(run_id, calls, drafts)


async def on_draft_decided(tenant_id: uuid.UUID, run_id: uuid.UUID, proposal_id: uuid.UUID) -> None:
    """An event message in the conversation when a Draft was confirmed / discarded / expired."""
    kinds = {
        "executed": "Done.",
        "execution_failed": "Couldn't complete the draft.",
        "discarded": "Discarded.",
        "expired": "This draft expired. Ask again.",
        "stale": "This draft is out of date. Ask again.",
    }
    async with tenant_session(tenant_id) as db:
        found = await store.turn_of_run(db, run_id)
        p = await db.get(AgentProposal, proposal_id)
        if found is None or p is None or p.origin != "portal" or p.status not in kinds:
            return
        conv = found[0]
        s = await settings_mod.get(tenant_id, db)
        seq = store.touch(conv, retention_days=s.retention_days)
        await store.add_message(
            db, conv, seq=seq, role="event",
            content={"kind": p.status, "proposal_id": str(p.id), "message": kinds[p.status], "error_code": p.error_code, "result": p.result if p.status == "executed" else None},
        )  # fmt: skip


__all__ = [
    "PORTAL_AGENT_KEYS",
    "draft_json",
    "drafts_of",
    "editable_fields",
    "on_draft_decided",
    "poll",
    "publish_artifacts",
    "record_answer",
]
