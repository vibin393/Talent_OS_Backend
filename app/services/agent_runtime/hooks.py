# ruff: file-ignore[line-too-long]
"""Agent runtime wiring (Epic 53), autodiscovered by ``core.hooks``.

* the 15 placeholder agents;
* post-discovery: ``validate_catalogue`` (startup fails on a human-only transition or an unknown tool) and the event
  trigger subscriptions;
* the workflow action ``agents.start_run``;
* notification types (``proposal_waiting``, ``proposal_expiring``, ``proposal_decided``, ``agent_run_failed``,
  ``agent_budget_threshold``, ``kill_switch_changed``, ``agent_nudge``, ``agent_flag``) and their event fan-out;
* step enqueueing (``RunStepRequested``) and wait resumption (``ProposalDecided`` -> ``proposal_decided``,
  ``SlotPicked`` -> ``slot_pick``);
* retention bindings (``agent_payloads``, ``agent_logs``) and audit action labels.
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import subscribe
from app.core.hooks import register_post_discovery
from app.services.agent_runtime.placeholders import register_placeholders

register_placeholders()


# ---------------------------------------------------------------- post-discovery
def _post_discovery() -> None:
    from app.core.agentic.tools import validate_catalogue
    from app.services.agent_runtime.triggers import wire_event_triggers
    from app.services.workflow import catalogue

    problems = validate_catalogue(is_human_only=catalogue.is_human_only)
    if problems:
        msg = "agent tool catalogue is invalid: " + "; ".join(problems)
        raise RuntimeError(msg)
    wire_event_triggers()


register_post_discovery(_post_discovery)


def _exposures_post_discovery() -> None:
    """Epic 66 (§0.15): validate the tool exposure registry (startup fails on a problem) and freeze the ``mcp``
    placeholder's allow-list / ceiling from the MCP exposures."""
    from app.core.agentic.exposure import check_exposures
    from app.services.agent_runtime.placeholders import refresh_mcp_spec

    check_exposures()
    refresh_mcp_spec()


register_post_discovery(_exposures_post_discovery)


# ---------------------------------------------------------------- workflow action
def _register_workflow() -> None:
    from app.services.agent_runtime.triggers import register_workflow_action

    register_workflow_action()


# ---------------------------------------------------------------- step enqueue / resume
def _subscribe() -> None:
    from app.events.agents import (
        AgentBudgetThresholdCrossed,
        AgentRunFailed,
        KillSwitchChanged,
        ProposalCreated,
        ProposalDecided,
    )
    from app.services.agent_runtime.runs import RunStepRequested

    def on_step(e: RunStepRequested) -> None:
        from app.tasks import agents as agent_tasks
        from app.tasks.runtime import _eager, enqueue

        if _eager(agent_tasks.run_step):
            enqueue(agent_tasks.run_step, str(e.tenant_id), str(e.run_id), e.step_no)
            return
        try:
            agent_tasks.run_step.apply_async(
                args=(str(e.tenant_id), str(e.run_id), e.step_no),
                queue=e.queue,
                countdown=e.countdown or None,
            )
        except Exception:
            logger.exception("Could not enqueue agent step {}:{}", e.run_id, e.step_no)

    def resume_on(condition: str, attr: str) -> Any:
        def handler(e: Any) -> None:
            from app.tasks import agents as agent_tasks
            from app.tasks.runtime import enqueue

            enqueue(agent_tasks.resume_event, str(e.tenant_id), condition, str(getattr(e, attr)))

        handler.__name__ = f"agents_resume_{condition}"
        return handler

    def fan_out(type_key: str, build: Any) -> Any:
        def handler(e: Any) -> None:
            from app.tasks import notifications as notification_tasks
            from app.tasks.runtime import enqueue

            payload = build(e)
            if payload is not None:
                enqueue(notification_tasks.fan_out, str(e.tenant_id), type_key, payload)

        handler.__name__ = f"agents_notify_{type_key}"
        return handler

    on_step.__name__ = "agents_on_run_step_requested"
    subscribe(RunStepRequested, on_step)
    subscribe(ProposalDecided, resume_on("proposal_decided", "proposal_id"))
    try:
        from app.events.scheduling import SlotPicked

        subscribe(SlotPicked, resume_on("slot_pick", "proposal_ref"))
    except ImportError:  # pragma: no cover - the contract event ships with the foundation
        logger.warning("SlotPicked event not available")
    subscribe(ProposalCreated, fan_out("proposal_waiting", lambda e: {
        "event_id": f"proposal:{e.proposal_id}", "proposal_id": str(e.proposal_id), "tool_key": e.tool_key,
        "agent_key": e.agent_key, "assignee_user_id": str(e.assignee_user_id) if e.assignee_user_id else None,
        "route_role_key": e.route_role_key,
    } if e.origin in ("agent", "external") else None))  # fmt: skip  # Epic 66: MCP Proposals reach their user too
    subscribe(ProposalDecided, fan_out("proposal_decided", lambda e: {
        "event_id": f"decided:{e.proposal_id}:{e.outcome}", "proposal_id": str(e.proposal_id), "outcome": e.outcome,
        "decided_by": str(e.decided_by) if e.decided_by else None,
    } if e.outcome in ("approved", "edited_approved", "rejected") else None))  # fmt: skip
    subscribe(AgentRunFailed, fan_out("agent_run_failed", lambda e: {
        "event_id": f"run_failed:{e.run_id}", "run_id": str(e.run_id), "agent_key": e.agent_key,
        "reason_code": e.reason_code, "owner_user_id": str(e.owner_user_id),
        "fallback_task_id": str(e.fallback_task_id) if e.fallback_task_id else None,
    } if e.agent_key != "copilot" and e.owner_user_id is not None else None))  # fmt: skip  # Copilot failures surface in the panel, not as notices; portal-owned runs have no user owner (Epic 66)
    subscribe(AgentBudgetThresholdCrossed, fan_out("agent_budget_threshold", lambda e: {
        "event_id": f"agent_budget:{e.agent_key}:{e.period}:{e.threshold}", "agent_key": e.agent_key,
        "threshold": e.threshold, "used": e.used, "budget": e.budget,
    }))  # fmt: skip
    subscribe(KillSwitchChanged, fan_out("kill_switch_changed", lambda e: {
        "event_id": f"kill:{e.scope}:{e.on}:{uuid.uuid4()}", "scope": e.scope, "on": e.on,
    }))  # fmt: skip


# ---------------------------------------------------------------- notifications
async def _holders(db: AsyncSession, *permissions: str) -> set[uuid.UUID]:
    """Active users holding any of ``permissions`` (+ Tenant Admins)."""
    from sqlalchemy import text

    rows = await db.execute(
        text(
            "SELECT DISTINCT ur.user_id FROM user_roles ur JOIN roles r ON r.id = ur.role_id "
            "JOIN users u ON u.id = ur.user_id WHERE u.is_active AND (r.key = 'tenant_admin' "
            "OR EXISTS (SELECT 1 FROM jsonb_object_keys(r.permissions) k WHERE k = ANY(:perms)))"
        ),
        {"perms": list(permissions)},
    )
    return {r[0] for r in rows}


def _register_notifications() -> None:
    from app.services import directory_service
    from app.services import notifications_service as ns

    def _uuid(v: Any) -> uuid.UUID | None:
        try:
            return uuid.UUID(str(v)) if v else None
        except ValueError:
            return None

    async def proposal_recipients(db: AsyncSession, payload: Any) -> set[uuid.UUID]:
        assignee = _uuid(payload.get("assignee_user_id"))
        if assignee is not None:
            return {assignee}
        role_key = payload.get("route_role_key")
        role_id = await directory_service.role_id_by_key(db, str(role_key)) if role_key else None
        return set(await directory_service.user_ids_with_role(db, role_id)) if role_id else set()

    async def render_proposal(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]  # NOSONAR
        pid = _uuid(payload.get("proposal_id"))
        title = (  # Epic 65 (FR-327): Collections Agent proposals are dunning drafts
            "Dunning draft waiting for your decision"
            if payload.get("agent_key") == "collections"
            else "An AI agent proposal is waiting for your decision"
        )
        return ns.Content(
            title=title,
            entity_type="agent_proposal",
            entity_id=pid,
            link_path=f"/proposals?id={pid}",
        )

    async def decided_recipients(db: AsyncSession, payload: Any) -> set[uuid.UUID]:
        from app.models.agent_runtime import AgentProposal

        pid = _uuid(payload.get("proposal_id"))
        p = await db.get(AgentProposal, pid) if pid else None
        if p is None or p.owner_user_id == _uuid(payload.get("decided_by")):
            return set()
        return {p.owner_user_id}  # type: ignore[arg-type]

    async def render_decided(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]  # NOSONAR
        pid = _uuid(payload.get("proposal_id"))
        outcome = "approved" if payload.get("outcome") != "rejected" else "rejected"
        return ns.Content(
            title=f"An AI agent proposal was {outcome}",
            entity_type="agent_proposal",
            entity_id=pid,
            link_path=f"/proposals?id={pid}",
        )

    async def owner(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # ruff: ignore[unused-function-argument]  # NOSONAR
        uid = _uuid(payload.get("owner_user_id"))
        return {uid} if uid else set()

    async def render_failed(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]  # NOSONAR
        task_id = _uuid(payload.get("fallback_task_id"))
        return ns.Content(
            title=f"An AI agent could not finish ({payload.get('reason_code')})",
            body="A task was created so you can continue manually." if task_id else None,
            entity_type="agent_run",
            entity_id=_uuid(payload.get("run_id")),
            link_path=f"/admin/ai/runs/{payload.get('run_id')}",
        )

    async def configurers(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # ruff: ignore[unused-function-argument]
        return await _holders(db, "ai:agent_configure")

    async def render_budget(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]  # NOSONAR
        return ns.Content(
            title=f"AI agent {payload.get('agent_key')} used {payload.get('threshold')}% of its monthly budget"
        )

    async def kill_recipients(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # ruff: ignore[unused-function-argument]
        return await _holders(db, "ai:agent_configure", "ai:kill_switch")

    async def render_kill(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]  # NOSONAR
        state = "stopped" if payload.get("on") else "resumed"
        return ns.Content(title=f"AI agents were {state} ({payload.get('scope')} switch)")

    async def pick_user(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # ruff: ignore[unused-function-argument]  # NOSONAR
        uid = _uuid(payload.get("user_id"))
        return {uid} if uid else set()

    async def render_message(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]  # NOSONAR
        etype = payload.get("entity_type")
        eid = _uuid(payload.get("entity_id"))
        from app.services import entity_refs

        return ns.Content(
            title=str(payload.get("message") or "AI agent")[:300],
            entity_type=etype,
            entity_id=eid,
            link_path=entity_refs.link_path(etype, eid),
        )

    defs = {
        "proposal_waiting": ns.NotificationType("AI proposal waiting", "AI agents", "both", False, proposal_recipients, render_proposal, internal_only=True),
        "proposal_expiring": ns.NotificationType("AI proposal expiring", "AI agents", "in_app", False, proposal_recipients, render_proposal, internal_only=True),
        "proposal_decided": ns.NotificationType("AI proposal decided", "AI agents", "in_app", False, decided_recipients, render_decided, internal_only=True),
        "agent_run_failed": ns.NotificationType("AI agent run failed", "AI agents", "in_app", False, owner, render_failed, internal_only=True),
        "agent_budget_threshold": ns.NotificationType("AI agent budget threshold", "AI agents", "both", False, configurers, render_budget, internal_only=True),
        "kill_switch_changed": ns.NotificationType("AI agents kill switch", "AI agents", "both", True, kill_recipients, render_kill, internal_only=True),
        "agent_nudge": ns.NotificationType("AI agent nudge", "AI agents", "in_app", False, pick_user, render_message, internal_only=True),
        "agent_flag": ns.NotificationType("AI agent flag", "AI agents", "in_app", False, pick_user, render_message, internal_only=True),
    }  # fmt: skip
    for key, definition in defs.items():
        if key not in ns.NOTIFICATION_TYPES:
            ns.register_notification_type(key, definition)


# ---------------------------------------------------------------- retention / audit
def _register_retention() -> None:
    from collections.abc import Sequence

    from sqlalchemy import delete, select, update

    from app.models.agent_runtime import AgentPayload, AgentProposal, AgentToolCall
    from app.services.privacy.retention_classes import register_retention_binding, retention_bindings

    async def _no_subject(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> None:  # ruff: ignore[unused-function-argument]  # NOSONAR
        return None

    def _anchor(model: Any) -> Any:
        async def anchor(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> Any:
            return (await db.execute(select(model.created_at).where(model.id == row_id))).scalar_one_or_none()

        return anchor

    async def expire_payloads(
        db: AsyncSession,
        ids: Sequence[uuid.UUID],
        action: str = "delete",  # ruff: ignore[unused-function-argument]
    ) -> dict[str, int]:
        result = await db.execute(delete(AgentPayload).where(AgentPayload.id.in_(list(ids))))
        return {"deleted": int(getattr(result, "rowcount", 0) or 0)}

    async def expire_calls(
        db: AsyncSession,
        ids: Sequence[uuid.UUID],
        action: str = "anonymize",  # ruff: ignore[unused-function-argument]
    ) -> dict[str, int]:
        result = await db.execute(
            update(AgentToolCall)
            .where(AgentToolCall.id.in_(list(ids)))
            .values(args_redacted=None, result_summary=None)
        )
        return {"redacted": int(getattr(result, "rowcount", 0) or 0)}

    async def expire_proposals(
        db: AsyncSession,
        ids: Sequence[uuid.UUID],
        action: str = "anonymize",  # ruff: ignore[unused-function-argument]
    ) -> dict[str, int]:
        result = await db.execute(
            update(AgentProposal)
            .where(AgentProposal.id.in_(list(ids)))
            .values(args={}, preview={"title": "[redacted]"}, result=None, rationale=None, evidence=[])
        )
        return {"redacted": int(getattr(result, "rowcount", 0) or 0)}

    bindings = retention_bindings()
    for table, class_key, model, fn in (
        ("agent_payloads", "agent_payloads", AgentPayload, expire_payloads),
        ("agent_tool_calls", "agent_logs", AgentToolCall, expire_calls),
        ("agent_proposals", "agent_logs", AgentProposal, expire_proposals),
    ):
        if table not in bindings:
            register_retention_binding(
                table, class_key, subject_resolver=_no_subject, anchor_resolver=_anchor(model), expire_fn=fn
            )


def _register_audit() -> None:
    from app.core import audit

    for key, text in (
        ("agent_run.started", "An AI agent run started"),
        ("agent_run.finished", "An AI agent run finished"),
        ("agent_run.cancelled", "An AI agent run was cancelled"),
        ("agent_run.payload_read", "AI agent run content was read (with a reason)"),
        ("agent_run.content_purged", "AI agent run content was erased"),
        ("agent_proposal.approved", "An AI agent proposal was approved"),
        ("agent_proposal.rejected", "An AI agent proposal was rejected"),
        ("agentic.enabled", "AI agents were enabled for the workspace"),
        ("agent.enabled", "An AI agent was enabled"),
        ("agent.disabled", "An AI agent was disabled"),
        ("agents.kill_switch", "The AI agents kill switch changed"),
        ("agents.platform_kill_switch", "The platform AI agents kill switch changed for the workspace"),
        ("agents.global_kill_switch", "The global AI agents kill switch changed"),
    ):
        audit.register_audit_action(key, text)


for _fn in (_register_workflow, _subscribe, _register_notifications, _register_retention, _register_audit):
    try:
        _fn()
    except Exception:  # pragma: no cover - surfaced in the logs; startup continues like other domains
        logger.exception("agent_runtime hooks: {} failed", _fn.__name__)
        raise
