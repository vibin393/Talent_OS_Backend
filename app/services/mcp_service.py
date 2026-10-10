# ruff: noqa: E501
"""Governed MCP Server facade (Epic 73, AD-84, FR-375..FR-380). Routers and tasks call only this module.

* ``handle(request)`` -- the whole ``/api/mcp`` exchange: ``mcp_server.transport`` (protocol, no I/O) driven against
  ``_Backend`` (this module), which authenticates the delegated ``mcp`` token (``oauth_service``), applies the gates,
  keeps ``mcp_sessions`` (``mcp_store``) and runs every tool call through ``agent_runtime_service.execute_external_call``
  (validation, Effective Rights, policy, Proposals, logging, masking, untrusted wrapping). It never calls a tool.
* Administration: enable / disable (human only, notice), settings, sessions, holds, the tool catalogue.
* ``end_sessions`` (task ``mcp.end_sessions``) ends sessions on kill switches, disablement, uninstall and revocation.
"""

import dataclasses
import hashlib
import re
import secrets
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.responses import Response

from app.core import audit
from app.core.actors import actor_from_ctx, require_human
from app.core.context import RequestContext, get_context, reset_context, set_context
from app.core.database import tenant_session
from app.core.errors import ConflictError, DomainError, Forbidden, NotFound, Unauthorized, ValidationFailed
from app.core.events import emit
from app.core.permissions import Principal
from app.core.rate_tiers import tier
from app.core.settings import get_settings
from app.events.mcp import McpEnablementChanged, McpSessionEnded, McpSessionOpened, McpSessionPaused
from app.schemas.agentic import ActionPolicyIn, ExternalCallIn, ExternalSessionIn
from app.schemas.mcp import (
    McpEnableIn,
    McpGroupOut,
    McpHoldOut,
    McpMeOut,
    McpSessionOut,
    McpSessionPage,
    McpSessionQuery,
    McpSettingsIn,
    McpSettingsOut,
    McpToolOut,
)
from app.services import (
    agent_runtime_service,
    ai,
    directory_service,
    ecosystem_service,
    entity_refs,
    exposure_service,
    identity_bridge,
    metering_service,
    oauth_service,
)
from app.services import notifications_service as ns
from app.services.mcp_server import auth, catalog, detector, lifecycle, limits, results, transport
from app.services.mcp_server.jsonrpc import INVALID_PARAMS, RpcError
from app.services.mcp_server.ports import Caller, SessionGone, SessionRef
from app.services.mcp_store import holds, sessions, settings_row

NOTICE_TEXT = (
    "People with the 'Connect an external AI assistant' permission can connect an assistant such as Claude. The "
    "assistant can read the records that person can see and can create proposals that the person must approve. Data "
    "returned to the assistant is processed by the assistant's model provider, which your organisation chooses when it "
    "installs the assistant app. Decisions about candidates are never available to assistants."
)
# end_reason -> agent run close reason
RUN_CLOSE: dict[str, str] = {
    "client_closed": "completed", "idle": "timeout", "kill_switch": "kill_switch", "ai_kill_switch": "kill_switch",
    "mcp_disabled": "manual", "uninstalled": "manual", "authorization_revoked": "manual",
    "user_deactivated": "owner_unavailable", "bulk_read": "policy_violation", "admin": "manual",
}  # fmt: skip
AUDIT_ACTIONS: tuple[tuple[str, str], ...] = (
    ("mcp.enabled", "AI assistant access (MCP) turned on"),
    ("mcp.disabled", "AI assistant access (MCP) turned off"),
    ("mcp.settings_changed", "AI assistant access settings changed"),
    ("mcp.session_started", "An AI assistant session started"),
    ("mcp.session_ended", "An AI assistant session ended"),
    ("mcp.session_paused", "An AI assistant session was paused"),
    ("mcp.hold_released", "AI assistant access restored for a user"),
    ("mcp.origin_refused", "An AI assistant request from an unlisted origin was refused"),
    ("mcp.protocol_refused", "An AI assistant used an unsupported protocol version"),
)
_SAMPLED: dict[tuple[str, str], float] = {}


def _now() -> datetime:
    return datetime.now(UTC)


def _hash(session_id: str) -> bytes:
    return hashlib.sha256(session_id.encode("utf-8")).digest()


@dataclasses.dataclass(frozen=True, slots=True)
class _Gates:
    groups: frozenset[str]
    settings: Mapping[str, Any]
    calls_per_minute: int
    sandbox: bool


def _system_ctx(tenant_id: uuid.UUID) -> RequestContext:
    return RequestContext(tenant_id=tenant_id)


# ================================================================ protocol backend
class _Backend:
    def __init__(self) -> None:
        self._gates: _Gates | None = None

    # ------------------------------------------------------------ auth + gates (spec 0.2)
    async def authenticate(self, request: Request, token: str | None) -> Caller:
        if not token:
            raise auth.unauthorized()
        try:
            principal = await oauth_service.authenticate_bearer(request, token, resource="mcp")
        except Unauthorized as exc:
            raise auth.unauthorized("invalid_token", "The access token is invalid or expired", error="invalid_token") from exc
        except Forbidden as exc:
            raise auth.forbidden(exc.code or "ecosystem_suspended", auth.SUSPENDED_TEXT) from exc
        if principal.acting_user_id is None or principal.app_installation_id is None:
            raise auth.unauthorized("invalid_token", "A delegated AI assistant token is required", error="invalid_token")
        cache_key = principal.oauth_token_id or principal.app_installation_id
        cached = auth.gate_cache.get(cache_key)
        if cached is None:
            try:
                cached = await self._compute_gates(principal)
            except auth.GateRefusal as refusal:
                cached = refusal
            auth.gate_cache.put(cache_key, cached)
        if isinstance(cached, auth.GateRefusal):
            raise cached
        self._gates = cached
        app_name = "MCP client"
        return Caller(
            tenant_id=principal.tenant_id,  # type: ignore[arg-type]
            installation_id=principal.app_installation_id,
            user_id=principal.acting_user_id,
            app_id=principal.app_id,
            token_id=principal.oauth_token_id,
            scopes=frozenset(principal.oauth_scopes),
            permissions=dict(principal.permissions),
            app_name=app_name,
            sandbox=cached.sandbox,
        )

    async def _compute_gates(self, principal: Principal) -> _Gates:
        tenant_id = principal.tenant_id
        assert tenant_id is not None
        ctx = get_context()
        async with tenant_session(tenant_id) as db:
            eco = await ecosystem_service.state(ctx, db)
            if not eco.mcp_enabled:
                raise auth.GateRefusal(401, "mcp_disabled", auth.OFF_TEXT, www=auth.www_authenticate())
            agentic = await agent_runtime_service.agentic_state(ctx, db)
            if agentic.kill_switch or agentic.platform_kill_switch or agentic.global_kill_switch:
                raise auth.forbidden("kill_switch", auth.PAUSED_TEXT)
            if not agentic.enabled or not await ai.is_feature_enabled(db, "agent_mcp"):
                raise auth.forbidden("mcp_disabled", auth.OFF_TEXT)
            user = await entity_refs.principal_for_user(db, tenant_id, principal.acting_user_id)  # type: ignore[arg-type]
            if user is None:
                raise auth.unauthorized("invalid_token", "The user is not active", error="invalid_token")
            if not user.has("mcp:use"):
                raise auth.forbidden("mcp_use_missing", auth.NO_PERMISSION_TEXT)
            if await holds.active_for(db, principal.acting_user_id) is not None:  # type: ignore[arg-type]
                raise auth.forbidden("mcp_access_held", auth.HOLD_TEXT)
            if not any(s.startswith("mcp:") for s in principal.oauth_scopes):
                raise auth.insufficient_scope()
            values = await settings_row.values(db)
        tier_limit = tier(eco.rate_tier).mcp_calls_per_min_per_session
        own = values.get("calls_per_minute")
        return _Gates(
            groups=frozenset(eco.mcp_tool_groups),
            settings=values,
            calls_per_minute=min(int(own), tier_limit) if own else tier_limit,
            sandbox=bool(eco.sandbox),
        )

    def _g(self) -> _Gates:
        assert self._gates is not None
        return self._gates

    async def origin_allowed(self, caller: Caller, origin: str) -> bool:
        allowed = {o.lower().rstrip("/") for o in self._g().settings.get("allowed_origins") or []}
        if caller.sandbox:
            allowed.add(get_settings().frontend_url.lower().rstrip("/"))
        return origin.lower().rstrip("/") in allowed

    async def audit_refusal(self, caller: Caller | None, action: str, detail: Mapping[str, Any]) -> None:
        if caller is None:
            return
        key = (str(caller.tenant_id), action)
        now = _now().timestamp()
        if now - _SAMPLED.get(key, 0.0) < 60:  # sampled: 1 per minute per tenant
            return
        _SAMPLED[key] = now
        try:
            async with tenant_session(caller.tenant_id) as db:
                audit.record(action, "app_installation", caller.installation_id, session=db, **dict(detail))
        except Exception:  # pragma: no cover - audit sampling never breaks a refusal
            logger.exception("MCP refusal audit failed")

    # ------------------------------------------------------------ sessions
    async def open_session(
        self, caller: Caller, *, protocol_version: str, client_name: str, client_version: str | None
    ) -> tuple[str, SessionRef]:
        raw = "mcps_" + secrets.token_urlsafe(32)
        digest = _hash(raw)
        ctx = get_context()
        try:
            async with tenant_session(caller.tenant_id) as db:
                run_id = await agent_runtime_service.open_external_session(
                    ctx, db,
                    ExternalSessionIn(
                        installation_id=caller.installation_id, user_id=caller.user_id,
                        token_id=caller.token_id or caller.installation_id,
                        client_name=client_name[:500], mcp_session_id=digest.hex()[:40],
                    ),
                )  # fmt: skip
                name = sanitize_client_name(client_name)
                row = await sessions.create(
                    db, tenant_id=caller.tenant_id, run_id=run_id, installation_id=caller.installation_id,
                    user_id=caller.user_id, token_family_id=None, session_hash=digest, protocol_version=protocol_version,
                    client_name=name, client_version=client_version,
                )  # fmt: skip
                emit(db, McpSessionOpened(caller.tenant_id, row.id, run_id, caller.installation_id, caller.user_id))
                audit.record(
                    "mcp.session_started", "mcp_session", row.id, session=db, client_name=name,
                    protocol_version=protocol_version, app_installation_id=str(caller.installation_id),
                )  # fmt: skip
                if self._g().settings.get("notify_user_on_first_connection", True) and await sessions.first_for(
                    db, user_id=caller.user_id, installation_id=caller.installation_id
                ):
                    await _deliver(
                        db, "mcp_first_connection", [caller.user_id],
                        ns.Content(
                            title=f"{name} connected to Talent OS as you",
                            body="If this wasn't you, remove its access in Connected apps.", link_path="/me/connected-apps",
                        ),
                        event_id=f"mcp_first:{caller.installation_id}:{caller.user_id}",
                    )  # fmt: skip
        except DomainError as exc:
            if exc.code in ("kill_switch",):
                raise auth.forbidden("kill_switch", auth.PAUSED_TEXT) from exc
            if exc.code in ("feature_off", "agent_off"):
                raise auth.forbidden("mcp_disabled", auth.OFF_TEXT) from exc
            raise auth.forbidden(exc.code or "forbidden", auth.NO_PERMISSION_TEXT) from exc
        return raw, SessionRef(row.id, run_id, protocol_version, caller.installation_id, caller.user_id, name, digest.hex())

    async def find_session(self, caller: Caller, session_id: str) -> SessionRef:
        if not session_id.startswith("mcps_") or len(session_id) > 200:
            raise SessionGone("unknown")
        digest = _hash(session_id)
        async with tenant_session(caller.tenant_id) as db:
            row = await sessions.get_by_hash(db, digest)
            if row is None or row.installation_id != caller.installation_id or row.user_id != caller.user_id:
                raise SessionGone("unknown")
            if row.status != "active":
                raise SessionGone(row.end_reason or row.status)
            status, reason = await sessions.run_reason(db, row.run_id)
            if status is not None and status != "running":  # idle close / deadline / runtime kill switch (lazy mirror)
                end_reason = "idle" if reason in (None, "timeout") else ("ai_kill_switch" if reason == "kill_switch" else "admin")
                locked = await sessions.get(db, row.id, lock=True)
                if locked is not None and await sessions.end(db, locked, reason=end_reason):
                    _ended(db, locked, end_reason)
                stale = end_reason
            else:
                stale = None
                ref = SessionRef(row.id, row.run_id, row.protocol_version, row.installation_id, row.user_id, row.client_name, digest.hex())
        if stale is not None:
            raise SessionGone(stale)
        return ref

    async def end_session(self, caller: Caller, session: SessionRef, *, reason: str) -> None:
        ctx = get_context()
        async with tenant_session(caller.tenant_id) as db:
            row = await sessions.get(db, session.id, lock=True)
            if row is None or not await sessions.end(db, row, reason=reason):
                return
            await agent_runtime_service.close_external_session(ctx, db, row.run_id, reason=RUN_CLOSE[reason])
            _ended(db, row, reason)

    # ------------------------------------------------------------ tools
    def _callable(self, caller: Caller) -> list[tuple[Any, Any]]:
        return catalog.callable_tools(groups=self._g().groups, scopes=caller.scopes, permissions=caller.permissions)

    async def list_tools(self, caller: Caller, session: SessionRef) -> dict[str, Any]:
        async with tenant_session(caller.tenant_id) as db:
            row = await sessions.get(db, session.id)
            if row is not None:
                await sessions.touch(db, row)
        return catalog.list_result(self._callable(caller))

    async def call_tool(
        self, caller: Caller, session: SessionRef, *, name: str, arguments: dict[str, Any], request_id: str | int | None
    ) -> dict[str, Any]:
        found = catalog.lookup(name)
        callable_keys = {t.key for t, _e in self._callable(caller)}
        ctx = get_context()
        call_id = f"rpc:{request_id}"[:64]
        if found is None:
            # Logged on the run (the runtime refuses an unregistered / unexposed name), then "Unknown tool".
            key = name.replace("__", ".", 1)[:80]
            if len(key) >= 3:
                try:
                    async with tenant_session(caller.tenant_id) as db:
                        await agent_runtime_service.execute_external_call(
                            ctx, db, ExternalCallIn(run_id=session.run_id, tool_key=key, args={}, call_id=call_id)
                        )
                        row = await sessions.get(db, session.id)
                        if row is not None:
                            await sessions.touch(db, row, call_count=1, refusal_count=1, violation_count=1)
                except DomainError:
                    logger.warning("MCP unknown-tool logging failed for {}", key)
            raise RpcError(INVALID_PARAMS, "Unknown tool", {"name": name[:100]})
        tool, exp = found
        if tool.key not in callable_keys:
            # Outside the session's callable set (group off, scope or permission missing): the runtime logs the refusal
            # (policy_violation / insufficient_scope / no_permission) and the client sees "Unknown tool".
            async with tenant_session(caller.tenant_id) as db:
                out = await agent_runtime_service.execute_external_call(
                    ctx, db, ExternalCallIn(run_id=session.run_id, tool_key=tool.key, args=arguments, call_id=call_id)
                )
                row = await sessions.get(db, session.id)
                if out.outcome in ("refused", "invalid", "not_found", "failed"):
                    if row is not None:
                        await sessions.touch(db, row, call_count=1, refusal_count=1, violation_count=1)
                    refused = True
                else:  # the live state already allows it (a stale gate cache): the runtime's answer stands
                    refused = False
            if refused:
                raise RpcError(INVALID_PARAMS, "Unknown tool", {"name": name[:100]})
            result, _deltas = results.map_outcome(
                out, scopes=caller.scopes, max_results=int(self._g().settings.get("max_results") or 50), mcp_scope=exp.mcp_scope
            )
            return result
        gates = self._g()
        for refusal in (
            await limits.check_minute(session.key, gates.calls_per_minute),
            await limits.check_day(caller.tenant_id, caller.user_id, int(gates.settings.get("calls_per_user_day") or 2000)),
            await limits.check_proposals(session.key, int(gates.settings.get("proposals_per_hour") or 25))
            if tool.risk != "read"
            else None,
        ):
            if refusal is not None:
                meta = {"talentos/retry_after": refusal.retry_after} if refusal.retry_after else None
                async with tenant_session(caller.tenant_id) as db:
                    row = await sessions.get(db, session.id)
                    if row is not None:
                        await sessions.touch(db, row, refusal_count=1)
                return results.text_result(refusal.text, is_error=True, meta=meta)
        async with tenant_session(caller.tenant_id) as db:
            out = await agent_runtime_service.execute_external_call(
                ctx, db, ExternalCallIn(run_id=session.run_id, tool_key=tool.key, args=arguments, call_id=call_id)
            )
            row = await sessions.get(db, session.id, lock=True)
            if out.outcome == "refused" and out.error_code in ("session_closed", "kill_switch"):
                reason = "ai_kill_switch" if out.error_code == "kill_switch" else "idle"
                if row is not None and await sessions.end(db, row, reason=reason):
                    _ended(db, row, reason)
                if out.error_code == "session_closed":
                    raise SessionGone(reason)
            proposal_text = None
            if out.outcome == "proposed":
                names = await directory_service.user_names(db, [caller.user_id])
                human = (out.result or {}).get("proposal") or "a proposal"
                proposal_text = (
                    f"Created proposal {human} for {names.get(caller.user_id) or 'you'} to review in Talent OS: "
                    f"{out.proposal_url}. Nothing has been done yet."
                )
            result, deltas = results.map_outcome(
                out, scopes=caller.scopes, max_results=int(gates.settings.get("max_results") or 50),
                mcp_scope=exp.mcp_scope, proposal_text=proposal_text,
            )  # fmt: skip
            if out.outcome == "proposed":
                await limits.add_proposal(session.key)
            paused = False
            if out.outcome == "executed" and out.candidate_ids and row is not None:
                for cid in dict.fromkeys(out.candidate_ids):
                    try:
                        await exposure_service.record(
                            ctx, subject_type="candidate", subject_id=cid, channel="mcp", installation_id=caller.installation_id
                        )
                    except Exception:  # pragma: no cover - exposure buffering never fails a call
                        logger.exception("MCP exposure record failed")
                threshold = int(gates.settings.get("bulk_read_threshold") or 500)
                obs = await detector.observe(session.key, out.candidate_ids, threshold)
                await sessions.set_distinct(db, row, obs.distinct)
                if obs.tripped:
                    paused = True
                    await _pause(ctx, db, row, caller, obs.distinct)
            if row is not None:
                await sessions.touch(db, row, call_count=1, **deltas)
        await metering_service.record(ctx, "mcp_tool_call", 1, caller.installation_id, error=bool(result.get("isError")))
        if paused:
            auth.gate_cache.clear()
            return results.text_result(
                "This session was paused because it read an unusual number of candidate records in an hour. "
                "Your AI Administrator can restore access.",
                is_error=True,
            )
        return result


_CONTROL = re.compile(r"[\x00-\x1f\x7f<>]+")


def sanitize_client_name(name: str) -> str:
    """``clientInfo`` name: printable, no markup, whitespace collapsed, at most 120 characters."""
    cleaned = " ".join(_CONTROL.sub(" ", name or "").split())
    return (cleaned or "MCP client")[:120]


def _ended(db: AsyncSession, row: Any, reason: str) -> None:
    emit(db, McpSessionEnded(row.tenant_id, row.id, row.run_id, row.installation_id, row.user_id, reason))
    audit.record("mcp.session_ended", "mcp_session", row.id, session=db, reason=reason)


async def _pause(ctx: RequestContext, db: AsyncSession, row: Any, caller: Caller, distinct: int) -> None:
    """Bulk-read detector tripped: pause, stop the run, hold the user, notify (spec 0.6)."""
    await sessions.end(db, row, reason="bulk_read", status="paused")
    await agent_runtime_service.close_external_session(ctx, db, row.run_id, reason="policy_violation")
    await holds.open_(
        db, tenant_id=caller.tenant_id, user_id=caller.user_id, installation_id=caller.installation_id, session_id=row.id,
        reason="bulk_read", auto_release_hours=get_settings().mcp_hold_auto_release_hours,
    )  # fmt: skip
    emit(db, McpSessionPaused(caller.tenant_id, row.id, row.run_id, caller.user_id, "bulk_read", distinct))
    audit.record("mcp.session_paused", "mcp_session", row.id, session=db, reason="bulk_read", distinct_candidates=distinct)
    names = await directory_service.user_names(db, [caller.user_id])
    who = names.get(caller.user_id) or "A user"
    admins = await _holders(db, "ai:agent_configure", "ai:govern")
    await _deliver(
        db, "mcp_session_paused", admins - {caller.user_id},
        ns.Content(
            title="An AI assistant session was paused",
            body=f"{who}'s assistant {row.client_name} read {distinct} candidate records in an hour.",
            link_path="/admin/ai/mcp-sessions",
        ),
        event_id=f"mcp_paused:{row.id}",
    )  # fmt: skip
    await _deliver(
        db, "mcp_session_paused", [caller.user_id],
        ns.Content(
            title="An AI assistant session was paused",
            body="Your AI assistant session was paused because it read an unusual number of records.",
            link_path="/me/ai-assistants",
        ),
        event_id=f"mcp_paused_user:{row.id}",
    )  # fmt: skip


async def _holders(db: AsyncSession, *permissions: str) -> set[uuid.UUID]:
    out: set[uuid.UUID] = set()
    for perm in permissions:
        for row in await identity_bridge.users_with_permission(db, perm):
            out.add(uuid.UUID(str(row["id"])))
    return out


async def _deliver(db: AsyncSession, key: str, recipients: Any, content: Any, *, event_id: str) -> None:
    ids = [r for r in recipients if r is not None]
    if not ids:
        return
    try:
        async with db.begin_nested():
            await ns.deliver(db, key, ids, content, event_id=event_id)
    except Exception:  # pragma: no cover - a notification never breaks the MCP call
        logger.exception("MCP notification {} failed", key)


# ================================================================ protocol entry points
async def handle(request: Request) -> Response:
    return await transport.handle(request, _Backend())


def protected_resource_metadata() -> dict[str, Any]:
    s = get_settings()
    return {
        "resource": s.p5_mcp_resource_url(),
        "authorization_servers": [s.p5_oauth_issuer()],
        "scopes_supported": ["mcp:read", "mcp:draft", "mcp:propose", "offline_access"],
        "bearer_methods_supported": ["header"],
        "resource_name": "Talent OS",
        "resource_documentation": f"{s.frontend_url.rstrip('/')}/developers/docs/mcp",
    }


# ================================================================ administration
def _principal(ctx: RequestContext) -> Principal:
    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.type != "user":
        raise Forbidden("A signed-in person is required", code="human_only_action")
    return principal


def _draft_tools() -> list[str]:
    return [t.key for t, e in catalog.exposed() if e.mcp_scope == "mcp:draft"]


async def _low_modes(ctx: RequestContext, db: AsyncSession) -> dict[str, str]:
    drafts = set(_draft_tools())
    modes = dict.fromkeys(sorted(drafts), "suggest")
    try:
        for entry in await agent_runtime_service.get_agents(ctx, db):
            if entry.key == "mcp":
                for t in entry.tools:
                    if t.key in drafts and t.mode == "auto":
                        modes[t.key] = "auto"
    except DomainError:  # pragma: no cover - catalogue read is best effort
        pass
    return modes


async def get_settings_out(ctx: RequestContext, db: AsyncSession) -> McpSettingsOut:
    eco = await ecosystem_service.state(ctx, db)
    agentic = await agent_runtime_service.agentic_state(ctx, db)
    values = await settings_row.values(db)
    tier_limit = tier(eco.rate_tier).mcp_calls_per_min_per_session
    enabled = set(eco.mcp_tool_groups)
    groups = [
        McpGroupOut(key=key, label=label, description=desc, enabled=key in enabled, tools=[t for t in _tool_rows() if t.group == key])
        for key, (label, desc) in catalog.MCP_TOOL_GROUPS.items()
    ]
    return McpSettingsOut(
        enabled=eco.mcp_enabled, enabled_at=eco.mcp_enabled_at, notice_version=eco.mcp_notice_version,
        current_notice_version=get_settings().mcp_notice_version, notice_text=NOTICE_TEXT, agentic_enabled=agentic.enabled,
        kill_switch=agentic.kill_switch or agentic.platform_kill_switch or agentic.global_kill_switch,
        tool_groups=sorted(enabled), groups=groups, calls_per_minute=values["calls_per_minute"],
        tier_calls_per_minute=tier_limit, calls_per_user_day=values["calls_per_user_day"],
        proposals_per_hour=values["proposals_per_hour"], bulk_read_threshold=values["bulk_read_threshold"],
        max_results=values["max_results"], allowed_origins=list(values["allowed_origins"] or []),
        notify_user_on_first_connection=values["notify_user_on_first_connection"],
        low_tool_modes=await _low_modes(ctx, db),  # type: ignore[arg-type]
        server_url=get_settings().p5_mcp_resource_url(),
    )  # fmt: skip


def _check_groups(groups: list[str]) -> list[str]:
    unknown = [g for g in groups if g not in catalog.MCP_TOOL_GROUPS]
    if unknown or not groups:
        raise ValidationFailed("Choose at least one known tool group", code="invalid_tool_groups", details={"unknown": unknown})
    return sorted(set(groups))


async def _notify_enablement(db: AsyncSession, on: bool, tenant_id: uuid.UUID) -> None:
    recipients = await _holders(db, "ai:agent_configure")
    await _deliver(
        db, "mcp_enabled_changed", recipients,
        ns.Content(
            title="AI assistant access was turned on" if on else "AI assistant access was turned off",
            link_path="/admin/ai/mcp",
        ),
        event_id=f"mcp_enabled:{tenant_id}:{_now().isoformat()}",
    )  # fmt: skip


async def enable(ctx: RequestContext, db: AsyncSession, data: McpEnableIn) -> McpSettingsOut:
    require_human(actor_from_ctx(ctx))
    principal = _principal(ctx)
    if not data.acknowledged:
        raise ValidationFailed("Acknowledge the notice first", code="notice_required", details={"fields": {"acknowledged": "Required"}})
    if data.notice_version != get_settings().mcp_notice_version:
        raise ConflictError("The notice has changed; review it again", code="notice_outdated")
    groups = _check_groups(data.tool_groups)
    agentic = await agent_runtime_service.agentic_state(ctx, db)
    if not agentic.enabled:
        raise ConflictError("Turn on AI agents first in Admin → AI", code="agentic_not_enabled")
    if agentic.kill_switch or agentic.platform_kill_switch or agentic.global_kill_switch:
        raise ConflictError("AI agents are paused by the kill switch", code="kill_switch_on")
    tenant_id = ctx.tenant_id
    assert tenant_id is not None
    await ecosystem_service.set_mcp_enabled(ctx, db, on=True, notice_version=data.notice_version, tool_groups=groups)
    # The ``mcp`` AgentSpec is the runtime's placeholder (its tools are the exposures), so the feature is set directly.
    await ai.set_features(db, tenant_id, {"agent_mcp": True})
    await settings_row.ensure(db, tenant_id)
    emit(db, McpEnablementChanged(tenant_id, True, tuple(groups), data.notice_version, principal.id))
    audit.record("mcp.enabled", "ecosystem_settings", tenant_id, session=db, notice_version=data.notice_version, tool_groups=groups)
    await _notify_enablement(db, True, tenant_id)
    auth.gate_cache.clear()
    await db.flush()
    return await get_settings_out(ctx, db)


async def disable(ctx: RequestContext, db: AsyncSession, *, reason: str) -> McpSettingsOut:
    require_human(actor_from_ctx(ctx))
    principal = _principal(ctx)
    tenant_id = ctx.tenant_id
    assert tenant_id is not None
    eco = await ecosystem_service.state(ctx, db)
    await ecosystem_service.set_mcp_enabled(ctx, db, on=False, notice_version=None, tool_groups=list(eco.mcp_tool_groups))
    await ai.set_features(db, tenant_id, {"agent_mcp": False})
    emit(db, McpEnablementChanged(tenant_id, False, tuple(eco.mcp_tool_groups), None, principal.id))
    audit.record("mcp.disabled", "ecosystem_settings", tenant_id, session=db, reason=reason[:500])
    await _notify_enablement(db, False, tenant_id)
    auth.gate_cache.clear()
    await db.flush()
    return await get_settings_out(ctx, db)


async def update_settings(ctx: RequestContext, db: AsyncSession, data: McpSettingsIn) -> McpSettingsOut:
    require_human(actor_from_ctx(ctx))
    principal = _principal(ctx)
    tenant_id = ctx.tenant_id
    assert tenant_id is not None
    before = await settings_row.values(db)
    changes: dict[str, Any] = {}
    for key in ("calls_per_user_day", "proposals_per_hour", "bulk_read_threshold", "max_results", "allowed_origins", "notify_user_on_first_connection"):
        value = getattr(data, key)
        if value is not None:
            changes[key] = value
    if data.calls_per_minute is not None:
        changes["calls_per_minute"] = data.calls_per_minute
    elif data.clear_calls_per_minute:
        changes["calls_per_minute"] = None
    if data.low_tool_modes:
        drafts = set(_draft_tools())
        bad = sorted(k for k in data.low_tool_modes if k not in drafts)
        if bad:
            raise ValidationFailed("Only the creating tools can run without approval", code="not_configurable", details={"tools": bad})
    if data.tool_groups is not None:
        groups = _check_groups(data.tool_groups)
        eco = await ecosystem_service.state(ctx, db)
        if not eco.mcp_enabled:
            raise ConflictError("Turn on AI assistant access first", code="mcp_not_enabled")
        await ecosystem_service.set_mcp_enabled(ctx, db, on=True, notice_version=eco.mcp_notice_version, tool_groups=groups)
        changes_groups = groups
    else:
        changes_groups = None
    if changes:
        await settings_row.upsert(db, tenant_id, changes, by=principal.id)
    for key, mode in (data.low_tool_modes or {}).items():
        await agent_runtime_service.set_action_policy(ctx, db, "mcp", key, ActionPolicyIn(mode=mode))
    audit.record(
        "mcp.settings_changed", "mcp_settings", tenant_id, session=db,
        before={k: before.get(k) for k in changes}, after=changes, tool_groups=changes_groups,
        low_tool_modes=data.low_tool_modes or None,
    )  # fmt: skip
    auth.gate_cache.clear()
    await db.flush()
    return await get_settings_out(ctx, db)


def _tool_rows() -> list[McpToolOut]:
    return [
        McpToolOut(
            key=t.key, name=t.wire_name, title=t.title, group=e.group, execution=catalog.execution_of(e),
            mcp_scope=e.mcp_scope or "", risk=t.risk,
        )
        for t, e in catalog.exposed()
    ]


async def tool_catalogue(ctx: RequestContext, db: AsyncSession) -> list[McpToolOut]:  # noqa: ARG001
    return _tool_rows()


def _session_out(row: Any, names: Mapping[uuid.UUID, str]) -> McpSessionOut:
    return McpSessionOut(
        id=row.id, run_id=row.run_id, installation_id=row.installation_id, user_id=row.user_id, user_name=names.get(row.user_id),
        client_name=row.client_name, client_version=row.client_version, protocol_version=row.protocol_version, status=row.status,
        end_reason=row.end_reason, started_at=row.started_at, last_seen_at=row.last_seen_at, ended_at=row.ended_at,
        call_count=row.call_count, proposal_count=row.proposal_count, refusal_count=row.refusal_count,
        violation_count=row.violation_count, redaction_count=row.redaction_count, distinct_candidates=row.distinct_candidates,
    )  # fmt: skip


async def list_sessions(ctx: RequestContext, db: AsyncSession, q: McpSessionQuery) -> McpSessionPage:
    principal = _principal(ctx)
    governs = principal.has("ai:govern")
    user_id = q.user_id if governs and not q.mine else principal.id
    rows, total = await sessions.list_(
        db, status=q.status, user_id=user_id, installation_id=q.installation_id, started_from=q.started_from,
        started_to=q.started_to, page=q.page, page_size=q.page_size,
    )  # fmt: skip
    names = await directory_service.user_names(db, {r.user_id for r in rows})
    return McpSessionPage(items=[_session_out(r, names) for r in rows], total=total, page=q.page, page_size=q.page_size)


async def end_session(ctx: RequestContext, db: AsyncSession, session_id: uuid.UUID, *, reason: str | None) -> McpSessionOut:
    principal = _principal(ctx)
    row = await sessions.get(db, session_id, lock=True)
    admin = principal.has("ai:agent_configure")
    if row is None or (row.user_id != principal.id and not admin):
        raise NotFound("Session not found")
    end_reason = "client_closed" if row.user_id == principal.id and not admin else "admin"
    if row.status != "ended":
        if row.status == "paused":
            row.status, row.ended_at = "ended", _now()
            await db.flush()
        elif await sessions.end(db, row, reason=end_reason):
            await agent_runtime_service.close_external_session(_system_ctx(row.tenant_id), db, row.run_id, reason=RUN_CLOSE[end_reason])
            _ended(db, row, end_reason)
            if reason:
                audit.record("mcp.session_ended", "mcp_session", row.id, session=db, reason=end_reason, note=reason[:500])
    names = await directory_service.user_names(db, [row.user_id])
    return _session_out(row, names)


def _hold_out(row: Any, names: Mapping[uuid.UUID, str]) -> McpHoldOut:
    return McpHoldOut(
        id=row.id, user_id=row.user_id, user_name=names.get(row.user_id), installation_id=row.installation_id,
        session_id=row.session_id, reason=row.reason, held_at=row.held_at, auto_release_at=row.auto_release_at,
        released_at=row.released_at, note=row.note,
    )  # fmt: skip


async def list_holds(ctx: RequestContext, db: AsyncSession) -> list[McpHoldOut]:  # noqa: ARG001
    rows = [r for r in await holds.list_open(db) if r.auto_release_at is None or r.auto_release_at > _now()]
    names = await directory_service.user_names(db, {r.user_id for r in rows})
    return [_hold_out(r, names) for r in rows]


async def release_hold(ctx: RequestContext, db: AsyncSession, hold_id: uuid.UUID, *, note: str) -> McpHoldOut:
    require_human(actor_from_ctx(ctx))
    principal = _principal(ctx)
    row = await holds.get(db, hold_id)
    if row is None or row.released_at is not None:
        raise NotFound("Hold not found")
    await holds.release(db, row, by=principal.id, note=note)
    audit.record("mcp.hold_released", "mcp_access_hold", row.id, session=db, user_id=str(row.user_id), note=note[:500])
    await _deliver(
        db, "mcp_hold_released", [row.user_id],
        ns.Content(title="AI assistant access restored", link_path="/me/ai-assistants"), event_id=f"mcp_hold_released:{row.id}",
    )  # fmt: skip
    auth.gate_cache.clear()
    names = await directory_service.user_names(db, [row.user_id])
    return _hold_out(row, names)


async def me(ctx: RequestContext, db: AsyncSession) -> McpMeOut:
    principal = _principal(ctx)
    eco = await ecosystem_service.state(ctx, db)
    return McpMeOut(
        enabled=eco.mcp_enabled, has_permission=principal.has("mcp:use"),
        on_hold=await holds.active_for(db, principal.id) is not None, server_url=get_settings().p5_mcp_resource_url(),
    )  # fmt: skip


# ================================================================ lifecycle (tasks)
async def end_sessions(
    tenant_id: uuid.UUID, *, reason: str, installation_id: uuid.UUID | None = None, user_id: uuid.UUID | None = None,
    app_id: uuid.UUID | None = None,
) -> int:  # fmt: skip
    """End every matching active session (spec 0.7). Runs in a system context; idempotent."""
    ctx = _system_ctx(tenant_id)
    token = set_context(ctx)
    try:
        async with tenant_session(tenant_id) as db:
            rows = await sessions.active(
                db, installation_ids=[installation_id] if installation_id else None, user_id=user_id, app_id=app_id
            )
            for row in rows:
                await sessions.end(db, row, reason=reason)
                await agent_runtime_service.close_external_session(ctx, db, row.run_id, reason=RUN_CLOSE[reason])
                _ended(db, row, reason)
            return len(rows)
    finally:
        reset_context(token)


async def rollup(tenant_id: uuid.UUID) -> dict[str, int]:
    """Beat ``mcp.detector_rollup``: mirror runs closed by the runtime (idle, deadline) and auto-release due holds."""
    async with tenant_session(tenant_id) as db:
        stale = await sessions.stale_active(db)
        for row, run_reason in stale:
            end_reason = "idle" if run_reason in (None, "timeout") else ("ai_kill_switch" if run_reason == "kill_switch" else "admin")
            if await sessions.end(db, row, reason=end_reason):
                _ended(db, row, end_reason)
        released = await holds.auto_release_due(db)
        for hold in released:
            audit.record("mcp.hold_released", "mcp_access_hold", hold.id, session=db, user_id=str(hold.user_id), note="auto")
    return {"mirrored": len(stale), "released": len(released)}


async def purge(tenant_id: uuid.UUID) -> int:
    from app.services.mcp_store import purge as purge_store

    async with tenant_session(tenant_id) as db:
        return await purge_store.purge_older_than(db, get_settings().mcp_session_retention_days)


# ================================================================ registrations (mcp_server.hooks)
_REGISTERED = {"done": False}
# MCP-only scopes (resource ``mcp``) that unlock the permissions of exposed tools no 66/67 base scope maps. Proposal
# tools stay Proposals whatever the scope (policy: ``mcp:propose`` exposures never execute directly).
E73_SCOPES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("knowledge:read", "Search knowledge sources", "search published knowledge sources", ("knowledge:read",)),
    ("predictions:read", "Read predictions", "read fill and risk predictions", ("prediction:read",)),
    ("messages:propose", "Propose messages", "propose messages that you approve before anything is sent", ("message:send_templated", "email:send")),
    ("interviews:propose", "Propose interview changes", "propose interview reschedules and cancellations that you approve", ("interview:schedule",)),
)


def _register_scopes() -> None:
    from app.core.oauth_scopes import ScopeSpec, register_scope, scopes

    for key, label, what, perms in E73_SCOPES:
        if key in scopes():
            continue
        register_scope(
            ScopeSpec(
                key=key, label=label, description=f"Lets the assistant {what}.", purpose_text=f"To {what} through an AI assistant.",
                permissions=perms, resources=frozenset({"mcp"}), api_key_allowed=False, owner_epic="73",
            )
        )  # fmt: skip


def _on_event(event: Any) -> None:
    req = lifecycle.for_event(event)
    if req is None:
        return
    auth.gate_cache.clear()
    from app.tasks import mcp as mcp_tasks
    from app.tasks.runtime import enqueue

    enqueue(mcp_tasks.end_sessions, *req.task_args())


def register_hooks() -> None:
    if _REGISTERED["done"]:
        return
    _REGISTERED["done"] = True
    from app.core.events import subscribe
    from app.events.agents import KillSwitchChanged
    from app.events.ecosystem import (
        EcosystemKillSwitchChanged,
        InstallationChanged,
        OAuthTokenFamilyRevoked,
        UserGrantChanged,
    )
    from app.services.settings_service import register_settings_entry

    _register_scopes()
    for event_type in (KillSwitchChanged, EcosystemKillSwitchChanged, McpEnablementChanged, InstallationChanged, UserGrantChanged, OAuthTokenFamilyRevoked):
        subscribe(event_type, _on_event)
    register_settings_entry(
        "ai", "mcp", "AI assistant access (MCP)",
        "Let people use their own AI assistants with Talent OS tools, choose tool groups and limits",
        "/api/v1/mcp/settings", "ai:agent_configure", owner_epic="73",
    )  # fmt: skip
    for key, desc in AUDIT_ACTIONS:
        audit.register_audit_action(key, desc)

    async def _none(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # noqa: ARG001 - delivered directly
        return set()

    async def _render(db: AsyncSession, payload: Any) -> Any:  # noqa: ARG001
        return ns.Content(title=str(payload.get("title") or "AI assistant access"))

    defs = {
        "mcp_enabled_changed": ns.NotificationType("AI assistant access changed", "AI agents", "both", True, _none, _render, internal_only=True),
        "mcp_session_paused": ns.NotificationType("AI assistant session paused", "AI agents", "both", True, _none, _render, internal_only=True),
        "mcp_first_connection": ns.NotificationType("AI assistant connected as you", "AI agents", "in_app", False, _none, _render, internal_only=True),
        "mcp_hold_released": ns.NotificationType("AI assistant access restored", "AI agents", "in_app", False, _none, _render, internal_only=True),
    }
    for key, definition in defs.items():
        if key not in ns.NOTIFICATION_TYPES:
            ns.register_notification_type(key, definition)


__all__ = [
    "NOTICE_TEXT",
    "disable",
    "enable",
    "end_session",
    "end_sessions",
    "get_settings_out",
    "handle",
    "list_holds",
    "list_sessions",
    "me",
    "protected_resource_metadata",
    "purge",
    "register_hooks",
    "release_hold",
    "rollup",
    "tool_catalogue",
    "update_settings",
]
