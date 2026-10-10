# ruff: noqa: E501, ARG001
"""Portal Copilot facade (Epic 77, FR-396..FR-400, AD-89). The three portal routers, the admin router and the
``portal_copilot.*`` tasks call only this. One Copilot stack: the P4 runtime, streaming and Fake LLM are reused; the
portal agents ``copilot_client`` / ``copilot_vendor`` / ``copilot_candidate`` act only as the signed-in portal
principal through ``portal_*`` tools.

Sessions: conversation storage runs on an internal tenant session (the vault data key is internal-only) with explicit
owner filters; tool reads run on the owner's org-scoped session (runtime); Draft writes run as the live portal
principal on an org-scoped session (``portal_copilot.tooling``). Admins see metadata only.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from app.core import audit
from app.core.actors import actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError, Forbidden, NotFound, ValidationFailed
from app.core.events import emit
from app.core.pagination import Page
from app.core.permissions import Principal
from app.events.portal_copilot import PortalCopilotDraftConfirmed
from app.schemas.portal_copilot import (
    DraftResultOut,
    PortalConversationDetailOut,
    PortalConversationOut,
    PortalCopilotSettingsIn,
    PortalCopilotSettingsOut,
    PortalCopilotStatusOut,
    PortalFeedbackIn,
    PortalTurnOut,
    PortalUsageOut,
    PortalUsageRowOut,
)
from app.services import entity_refs
from app.services.copilot import streaming
from app.services.portal_copilot import answers, availability, consent, ratelimit, store, turns
from app.services.portal_copilot import settings as settings_mod
from app.services.portal_copilot.principals import AGENT_KEYS, PortalActor, actor_for, with_rights

DISCLOSURE = "Copilot is an AI assistant acting for {tenant}. It can see only what you can see here and can make mistakes."
KIND_OF_AGENT = {v: k for k, v in AGENT_KEYS.items()}


def _ctx(ctx: RequestContext, actor: PortalActor) -> RequestContext:
    return RequestContext(
        request_id=ctx.request_id,
        tenant_id=actor.tenant_id,
        principal=actor.core_principal(),
        ip=ctx.ip,
        user_agent=ctx.user_agent,
    )


async def _actor(kind: str, principal: Any) -> PortalActor:
    return await with_rights(actor_for(kind, principal))


# ================================================================ status / consent
async def status(ctx: RequestContext, kind: str, principal: Any) -> PortalCopilotStatusOut:
    actor = await _actor(kind, principal)
    async with tenant_session(actor.tenant_id) as idb:
        state = await availability.check(ctx, idb, actor)
        tenant = await entity_refs.tenant(idb, actor.tenant_id)
        notice = (
            await consent.candidate_notice(idb, actor.tenant_id)
            if state.reason == "consent_required"
            else None
        )
    s = state.settings
    limit = s.questions_per_hour(kind)
    left = (
        await ratelimit.remaining(actor.tenant_id, actor.principal_type, actor.principal_id, limit)
        if state.available
        else 0
    )
    return PortalCopilotStatusOut(
        portal=kind, available=state.available, reason=state.reason, tenant_name=tenant.name,  # type: ignore[arg-type]
        questions_per_hour=limit, questions_left_this_hour=left, retention_days=s.retention_days,
        disclosure=DISCLOSURE.format(tenant=tenant.name), disclosure_extra=s.disclosure_extra,
        consent_notice_id=notice[0] if notice else None, consent_notice_text=notice[1] if notice else None,
    )  # fmt: skip


async def grant_consent(ctx: RequestContext, principal: Any, notice_id: uuid.UUID) -> PortalCopilotStatusOut:
    actor = actor_for("candidate", principal)
    async with tenant_session(actor.tenant_id) as idb:
        await consent.grant(ctx, idb, actor, notice_id)
    return await status(ctx, "candidate", principal)


# ================================================================ conversations
async def list_conversations(
    ctx: RequestContext, kind: str, principal: Any, *, page: int = 1, page_size: int = 20
) -> Page[PortalConversationOut]:
    actor = await _actor(kind, principal)
    async with tenant_session(actor.tenant_id) as idb:
        rows, total = await store.list_conversations(
            idb, actor, page=max(page, 1), page_size=min(max(page_size, 1), 50)
        )
        items = [await turns.conversation_out(idb, c) for c in rows]
    return Page[PortalConversationOut](items=items, total=total, page=page, page_size=page_size)


async def create_conversation(ctx: RequestContext, kind: str, principal: Any) -> PortalConversationOut:
    actor = await _actor(kind, principal)
    async with tenant_session(actor.tenant_id) as idb:
        state = await turns.require_available(ctx, idb, actor)
        if kind != "candidate" and not await store.ids_of_principal(
            idb, actor.principal_type, actor.principal_id
        ):
            consent.acknowledge(idb, actor)  # first use: the portal_copilot notice was shown (A6)
        conv = await store.create_conversation(idb, actor, retention_days=state.settings.retention_days)
        return await turns.conversation_out(idb, conv)


async def get_conversation(
    ctx: RequestContext, kind: str, principal: Any, conversation_id: uuid.UUID
) -> PortalConversationDetailOut:
    actor = actor_for(kind, principal)
    async with tenant_session(actor.tenant_id) as idb:
        conv = await store.owned_conversation(idb, actor, conversation_id)
        return await turns.detail(idb, conv)


async def close(
    ctx: RequestContext, kind: str, principal: Any, conversation_id: uuid.UUID
) -> PortalConversationOut:
    actor = actor_for(kind, principal)
    async with tenant_session(actor.tenant_id) as idb:
        return await turns.close(idb, actor, conversation_id)


async def delete(ctx: RequestContext, kind: str, principal: Any, conversation_id: uuid.UUID) -> None:
    actor = actor_for(kind, principal)
    async with tenant_session(actor.tenant_id) as idb:
        await turns.delete(idb, actor, conversation_id)


# ================================================================ turns
async def post_message(
    ctx: RequestContext, kind: str, principal: Any, conversation_id: uuid.UUID, text: str
) -> PortalTurnOut:
    actor = await _actor(kind, principal)
    async with tenant_session(actor.tenant_id) as idb:
        return await turns.post_message(_ctx(ctx, actor), idb, actor, conversation_id, text)


async def stream(
    ctx: RequestContext, kind: str, principal: Any, run_id: uuid.UUID, *, after: str | None
) -> AsyncIterator[bytes]:
    """Owner check now (404 for anyone else), then the SSE generator (runs after the request)."""
    actor = actor_for(kind, principal)
    async with tenant_session(actor.tenant_id) as idb:
        await turns.owned_turn(idb, actor, run_id)
    tenant_id = actor.tenant_id

    async def poll() -> None:
        await answers.poll(tenant_id, run_id)

    return streaming.sse(run_id, last_event_id=after, poll=poll)


async def cancel(ctx: RequestContext, kind: str, principal: Any, run_id: uuid.UUID) -> None:
    actor = actor_for(kind, principal)
    async with tenant_session(actor.tenant_id) as idb:
        await turns.cancel(idb, actor, run_id)


async def feedback(
    ctx: RequestContext, kind: str, principal: Any, run_id: uuid.UUID, data: PortalFeedbackIn
) -> None:
    actor = actor_for(kind, principal)
    async with tenant_session(actor.tenant_id) as idb:
        conv, _msg = await turns.owned_turn(idb, actor, run_id)
        comment = (data.comment or "").strip() or None
        await store.upsert_feedback(
            idb, conv, actor, run_id, rating=data.rating, reason=data.reason, comment=comment
        )


# ================================================================ drafts
async def _own_draft(idb: Any, actor: PortalActor, proposal_id: uuid.UUID) -> Any:
    from app.models.agent_runtime import AgentProposal

    p = await idb.get(AgentProposal, proposal_id)
    if (
        p is None or p.origin != "portal" or p.owner_principal_type != actor.principal_type
        or p.owner_principal_id != actor.principal_id or p.owner_org_id != actor.org_id
    ):  # fmt: skip
        raise NotFound("Proposal not found")
    return p


def _result(p: Any) -> DraftResultOut:
    result = dict(p.result or {}) if p.result else None
    url = (result or {}).get("open_url") or (result or {}).get("url")
    return DraftResultOut(
        proposal_id=p.id, status=p.status, error_code=p.error_code, open_url=url, result=result
    )


async def confirm_draft(
    ctx: RequestContext, kind: str, principal: Any, proposal_id: uuid.UUID, *, edits: dict[str, Any] | None
) -> DraftResultOut:
    from app.services import agent_runtime_service as runtime

    actor = await _actor(kind, principal)
    pctx = _ctx(ctx, actor)
    require_human(actor_from_ctx(pctx))
    async with tenant_session(actor.tenant_id) as idb:
        p = await _own_draft(idb, actor, proposal_id)
        edited = None
        if edits:
            allowed = set(answers.editable_fields(dict(p.args or {})))
            bad = sorted(set(edits) - allowed)
            if bad:
                raise ValidationFailed(
                    "These fields cannot be edited", code="proposal_edit_not_allowed", details={"fields": bad}
                )
            edited = {**dict(p.args or {}), **edits}
        await runtime.confirm_draft(pctx, idb, proposal_id, edited_args=edited)
        p = await _own_draft(idb, actor, proposal_id)
        if p.status == "executed":
            emit(
                idb,
                PortalCopilotDraftConfirmed(
                    actor.tenant_id, p.id, p.tool_key, actor.principal_type, actor.principal_id
                ),
            )
        return _result(p)


async def discard_draft(
    ctx: RequestContext, kind: str, principal: Any, proposal_id: uuid.UUID
) -> DraftResultOut:
    from app.services import agent_runtime_service as runtime

    actor = actor_for(kind, principal)
    pctx = _ctx(ctx, actor)
    async with tenant_session(actor.tenant_id) as idb:
        await _own_draft(idb, actor, proposal_id)
        await runtime.discard_draft(pctx, idb, proposal_id)
        return _result(await _own_draft(idb, actor, proposal_id))


# ================================================================ run completion / retention (tasks)
async def record_answer(tenant_id: uuid.UUID, run_id: uuid.UUID) -> bool:
    return await answers.record_answer(tenant_id, run_id)


async def on_draft_decided(tenant_id: uuid.UUID, run_id: uuid.UUID, proposal_id: uuid.UUID) -> None:
    await answers.on_draft_decided(tenant_id, run_id, proposal_id)


async def purge_expired(tenant_id: uuid.UUID) -> int:
    async with tenant_session(tenant_id) as idb:
        count = await store.delete_conversations(idb, await store.expired_ids(idb))
        if count:
            audit.record(
                "portal_copilot.conversation_purged",
                "portal_copilot_conversation",
                None,
                session=idb,
                count=count,
            )
        return count


# ================================================================ admin (internal)
def _require(ctx: RequestContext, *keys: str) -> Principal:
    principal = ctx.principal
    if (
        not isinstance(principal, Principal)
        or principal.type != "user"
        or not any(principal.has(k) for k in keys)
    ):
        raise Forbidden("You do not have permission to manage the portal Copilot")
    return principal


async def _settings_out(db: Any, tenant_id: uuid.UUID) -> PortalCopilotSettingsOut:
    from app.services import ai

    s = await settings_mod.get(tenant_id, db)
    features = await ai.features_map(db)
    return PortalCopilotSettingsOut(
        **{f: getattr(s, f) for f in settings_mod.FIELDS},
        agentic_enabled=bool(features.get("agentic")),
        updated_at=s.updated_at,
    )


async def get_settings(ctx: RequestContext, db: Any) -> PortalCopilotSettingsOut:
    _require(ctx, "ai:govern", "ai:agent_configure")
    assert ctx.tenant_id is not None
    return await _settings_out(db, ctx.tenant_id)


async def update_settings(
    ctx: RequestContext, db: Any, data: PortalCopilotSettingsIn
) -> PortalCopilotSettingsOut:
    from app.services import ai

    require_human(actor_from_ctx(ctx))
    principal = _require(ctx, "ai:agent_configure")
    tenant_id = ctx.tenant_id
    assert tenant_id is not None
    changes = {k: getattr(data, k) for k in settings_mod.FIELDS if k in data.model_fields_set}
    for key in (
        "client_enabled",
        "vendor_enabled",
        "candidate_enabled",
        "questions_per_hour_external",
        "questions_per_hour_candidate",
        "retention_days",
    ):
        if key in changes and changes[key] is None:
            changes.pop(key)
    enabling = [k for k in ("client", "vendor", "candidate") if changes.get(f"{k}_enabled") is True]
    features = await ai.features_map(db)
    if enabling and not features.get("agentic"):
        raise ConflictError("Turn on AI agents first", code="agentic_disabled")
    if changes:
        await settings_mod.update(db, tenant_id, changes, principal.id)
        toggles = {
            f"agent_{AGENT_KEYS[k]}": bool(changes[f"{k}_enabled"])
            for k in ("client", "vendor", "candidate")
            if f"{k}_enabled" in changes
        }
        if toggles:
            await ai.set_features(db, tenant_id, toggles)
    return await _settings_out(db, tenant_id)


async def usage(
    ctx: RequestContext, db: Any, *, date_from: date | None, date_to: date | None, portal: str | None
) -> PortalUsageOut:
    """Metadata only: runs, tokens and cost (``agent_runs``), ratings and confirmed Drafts -- never content."""
    from app.models.agent_runtime import AgentProposal, AgentRun
    from app.models.portal_copilot import PortalCopilotFeedback

    _require(ctx, "ai:govern")
    today = datetime.now(UTC).date()
    date_to = date_to or today
    date_from = date_from or (date_to - timedelta(days=29))
    start = datetime.combine(date_from, datetime.min.time(), tzinfo=UTC)
    end = datetime.combine(date_to + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    rows: list[PortalUsageRowOut] = []
    for kind, agent_key in AGENT_KEYS.items():
        if portal and portal != kind:
            continue
        runs = (
            await db.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(AgentRun.input_tokens), 0),
                    func.coalesce(func.sum(AgentRun.output_tokens), 0),
                    func.coalesce(func.sum(AgentRun.cost_micros), 0),
                ).where(
                    AgentRun.agent_key == agent_key, AgentRun.created_at >= start, AgentRun.created_at < end
                )
            )
        ).one()
        principal_type = {"client": "client_user", "vendor": "vendor_user", "candidate": "candidate"}[kind]
        ratings = dict(
            (
                await db.execute(
                    select(PortalCopilotFeedback.rating, func.count())
                    .where(
                        PortalCopilotFeedback.principal_type == principal_type,
                        PortalCopilotFeedback.created_at >= start,
                        PortalCopilotFeedback.created_at < end,
                    )
                    .group_by(PortalCopilotFeedback.rating)
                )
            ).all()
        )
        confirmed = (
            await db.execute(
                select(func.count()).select_from(AgentProposal).where(
                    AgentProposal.agent_key == agent_key, AgentProposal.status == "executed",
                    AgentProposal.created_at >= start, AgentProposal.created_at < end,
                )
            )
        ).scalar_one()  # fmt: skip
        rated = int(ratings.get("up", 0)) + int(ratings.get("down", 0))
        rows.append(
            PortalUsageRowOut(
                portal=kind,
                questions=int(runs[0]),
                helpful_pct=round(100 * int(ratings.get("up", 0)) / rated, 1) if rated else None,  # type: ignore[arg-type]
                drafts_confirmed=int(confirmed),
                input_tokens=int(runs[1]),
                output_tokens=int(runs[2]),
                cost_usd=round(int(runs[3]) / 1_000_000, 4),
            )
        )
    return PortalUsageOut(date_from=date_from, date_to=date_to, rows=rows)


__all__ = [
    "DISCLOSURE",
    "cancel",
    "close",
    "confirm_draft",
    "create_conversation",
    "delete",
    "discard_draft",
    "feedback",
    "get_conversation",
    "get_settings",
    "grant_consent",
    "list_conversations",
    "on_draft_decided",
    "post_message",
    "purge_expired",
    "record_answer",
    "status",
    "stream",
    "update_settings",
    "usage",
]
