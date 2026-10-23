# ruff: file-ignore[line-too-long]
"""Portal Copilot conversations and turns (Epic 77 A4, AD-64 / AD-89).

``post_message`` order: availability (P4 kill switches / agentic / AI budget + portal enablement + consent) ->
principal permission -> ownership (404 for anyone else) -> open conversation -> rate limit (429
``copilot_rate_limited`` + ``Retry-After``) -> seal and store the question -> ``start_run(agent_key=copilot_<kind>,
origin=portal, trigger_kind=portal, owner=OwnerRef, dedup_key=pcopilot:<conv>:<seq>)`` on queue ``conversations``
-> ``turn.started`` -> ``metering_service.record(portal_copilot_question)`` -> ``stream_url``.

Every function takes an internal tenant session ``idb`` (store rule) and the ``PortalActor``."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.agentic import Kickoff
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, RateLimited
from app.schemas.portal_copilot import (
    PortalConversationDetailOut,
    PortalConversationOut,
    PortalMessageOut,
    PortalTurnOut,
)
from app.services import entity_refs, metering_service
from app.services.copilot import streaming
from app.services.copilot.context import prior_turns
from app.services.portal_copilot import availability, ratelimit, store
from app.services.portal_copilot.principals import PortalActor, owner_ref

STREAM_PATHS = {
    "client": "/api/v1/client-portal/copilot",
    "vendor": "/api/v1/vendor-portal/copilot",
    "candidate": "/api/v1/portal/copilot",
}
UNAVAILABLE_TEXT = {
    "kill_switch": "Copilot is paused right now.",
    "feature_off": "Copilot is not turned on for this portal.",
    "agent_not_available": "Copilot is not turned on for this portal.",
    "owner_unavailable": "Your access changed. Refresh the page.",
    "all_actions_off": "Your access changed. Refresh the page.",
}
PRINCIPAL_LABEL = {"client": "a Client User", "vendor": "a Vendor User", "candidate": "a Candidate"}


def stream_url(kind: str, run_id: uuid.UUID) -> str:
    return f"{STREAM_PATHS[kind]}/turns/{run_id}/events"


async def conversation_out(idb: AsyncSession, c: Any) -> PortalConversationOut:
    return PortalConversationOut(
        id=c.id, agent_key=c.agent_key, title=await store.title_of(idb, c), status=c.status, message_count=c.message_count,
        last_message_at=c.last_message_at, created_at=c.created_at, expires_at=c.expires_at,
    )  # fmt: skip


async def message_out(idb: AsyncSession, m: Any) -> PortalMessageOut:
    return PortalMessageOut(
        id=m.id,
        seq=m.seq,
        role=m.role,
        content=await store.content_of(idb, m),
        run_id=m.run_id,
        created_at=m.created_at,
    )


async def detail(idb: AsyncSession, conv: Any) -> PortalConversationDetailOut:
    msgs = [await message_out(idb, m) for m in await store.messages_of(idb, conv.id)]
    return PortalConversationDetailOut(**(await conversation_out(idb, conv)).model_dump(), messages=msgs)


def _unavailable(reason: str) -> ConflictError:
    return ConflictError(
        "Copilot is not available right now", code="copilot_unavailable", details={"reason": reason}
    )


async def require_available(ctx: Any, idb: AsyncSession, actor: PortalActor) -> availability.Availability:
    state = await availability.check(ctx, idb, actor)
    if state.reason == "no_permission":
        from app.core.errors import Forbidden

        raise Forbidden(
            "You do not have access to this area", code="portal_permission_required",
            details={"permission": "portal_copilot:use"},
        )  # fmt: skip
    if state.reason is not None:
        raise _unavailable(state.reason)
    return state


async def post_message(
    ctx: RequestContext, idb: AsyncSession, actor: PortalActor, conversation_id: uuid.UUID, text: str
) -> PortalTurnOut:
    state = await require_available(ctx, idb, actor)
    conv = await store.owned_conversation(idb, actor, conversation_id, lock=True)
    if conv.status != "open":
        raise ConflictError("This conversation is closed. Start a new chat.", code="conversation_closed")
    limit = state.settings.questions_per_hour(actor.kind)
    window = await ratelimit.hit(actor.tenant_id, actor.principal_type, actor.principal_id, limit)
    if not window.allowed:
        minutes = max(1, (window.retry_after_s + 59) // 60)
        exc = RateLimited(
            f"You've asked a lot of questions this hour. Try again in {minutes} minutes.", code="copilot_rate_limited",
            details={"retry_after_s": window.retry_after_s, "limit": limit},
        )  # fmt: skip
        exc.response_hooks.append(lambda r: r.headers.__setitem__("Retry-After", str(window.retry_after_s)))
        raise exc
    retention = state.settings.retention_days
    prior = [(m.role, await store.content_of(idb, m)) for m in await store.messages_of(idb, conv.id)]
    seq = store.touch(conv, retention_days=retention)
    if seq == 1:
        await store.set_title(idb, conv, text)
    user_msg = await store.add_message(idb, conv, seq=seq, role="user", content={"text": text})
    tenant = await entity_refs.tenant(idb, actor.tenant_id)
    history, _cut = prior_turns(prior, summary=None)
    kickoff = Kickoff(
        inputs={
            "question": text[:4000], "tenant": tenant.name, "principal_label": PRINCIPAL_LABEL[actor.kind],
            "today": datetime.now(tenant.tz).date().isoformat(),
        },
        untrusted={"prior_turns": history} if history else {},
    )  # fmt: skip
    from app.services import agent_runtime_service as runtime

    ref = await runtime.start_run(
        ctx, idb, agent_key=actor.agent_key, trigger_kind="portal", origin="portal", owner=owner_ref(actor),
        conversation_id=conv.id, dedup_key=f"pcopilot:{conv.id}:{seq}", kickoff=kickoff,
    )  # fmt: skip
    if ref.run_id is None or ref.status in ("not_started", "skipped"):
        reason = ref.reason or "not_started"
        seq2 = store.touch(conv, retention_days=retention)
        await store.add_message(
            idb, conv, seq=seq2, role="assistant",
            content={"text": UNAVAILABLE_TEXT.get(reason, "Copilot couldn't start this answer. Try again."), "status": "unavailable", "reason_code": reason, "links": [], "drafts": []},
        )  # fmt: skip
        return PortalTurnOut(
            message=await message_out(idb, user_msg),
            run_id=None,
            stream_url=None,
            status="unavailable",
            reason=reason,
        )
    user_msg.run_id = ref.run_id
    await idb.flush()
    await streaming.publish(
        ref.run_id,
        "turn.started",
        {"run_id": str(ref.run_id), "conversation_id": str(conv.id), "seq": seq},
        once="started",
    )
    await metering_service.record(RequestContext(tenant_id=actor.tenant_id), "portal_copilot_question")
    audit.record(
        "portal_copilot.turn_started",
        "portal_copilot_conversation",
        conv.id,
        session=idb,
        agent_key=actor.agent_key,
    )
    return PortalTurnOut(
        message=await message_out(idb, user_msg),
        run_id=ref.run_id,
        stream_url=stream_url(actor.kind, ref.run_id),
        status=ref.status,
        reason=ref.reason,
    )


async def owned_turn(idb: AsyncSession, actor: PortalActor, run_id: uuid.UUID) -> tuple[Any, Any]:
    found = await store.turn_of_run(idb, run_id)
    if found is None:
        raise NotFound("Turn not found")
    conv, msg = found
    await store.owned_conversation(idb, actor, conv.id)  # 404 for anyone else
    return conv, msg


async def cancel(idb: AsyncSession, actor: PortalActor, run_id: uuid.UUID) -> None:
    from app.services import agent_runtime_service as runtime

    await owned_turn(idb, actor, run_id)
    await runtime.cancel_run(
        RequestContext(tenant_id=actor.tenant_id), idb, run_id, reason="Stopped in the portal Copilot"
    )


def _now() -> datetime:
    return datetime.now(UTC)


async def close(idb: AsyncSession, actor: PortalActor, conversation_id: uuid.UUID) -> PortalConversationOut:
    conv = await store.owned_conversation(idb, actor, conversation_id, lock=True)
    if conv.status != "closed":
        conv.status, conv.closed_at = "closed", _now()
        await idb.flush()
    return await conversation_out(idb, conv)


async def delete(idb: AsyncSession, actor: PortalActor, conversation_id: uuid.UUID) -> None:
    conv = await store.owned_conversation(idb, actor, conversation_id, lock=True)
    await store.delete_conversations(idb, [conv.id])
    audit.record(
        "portal_copilot.conversation_deleted", "portal_copilot_conversation", conversation_id, session=idb
    )


__all__ = [
    "UNAVAILABLE_TEXT",
    "cancel",
    "close",
    "conversation_out",
    "delete",
    "detail",
    "message_out",
    "owned_turn",
    "post_message",
    "require_available",
    "stream_url",
]
