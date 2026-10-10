# ruff: noqa: E501, ARG001, N812
"""Phase 4 list kinds (Epic 65 A8, FR-329): ``agent_runs`` and ``agent_proposals`` for the two kinds allowed by
``ck_exports_kind`` (foundation 0.3), built on the ``p4tables`` projections (reporting imports no Phase 4 module).

The P1 export engine is unchanged (50k rows, 5k sync, formula-injection guard, audit ``export.run``). Never exported:
run ``result``, payloads, tool arguments or summaries; Proposal ``args``, ``preview``, ``rationale``, ``evidence``,
``result`` or ``decision_note`` (not even projected).

Visibility: ``agent_runs`` needs ``ai:govern`` (tenant). ``agent_proposals`` -- ``ai:govern`` at tenant scope sees every
agent Proposal; ``proposal:decide`` holders see the ones assigned to them, owned by them or in a role queue they belong
to (the search rule). Copilot drafts are never listed.
"""

from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Select, String, Uuid, column, false, or_, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.permissions import Principal, Scope
from app.services.reporting import lists
from app.services.reporting.common import users
from app.services.reporting.lists import ALT_READ_PERMISSIONS, Column, ListKindDef, register_list_kind
from app.services.reporting.lists_p2 import P2ListFilters, _common, _need
from app.services.reporting.p4tables import _t
from app.services.reporting.p4tables import agent_runs as R

_roles = table("roles", column("id", Uuid()), column("key", String()))
P = _t(
    "agent_proposals",
    "id:u human_id:s origin:s agent_key:s tool_key:s title:s risk:s status:s edited:b target_type:s target_id:u "
    "assignee_user_id:u route_role_key:s owner_user_id:u created_at:t expires_at:t decided_by:u decided_at:t "
    "executed_at:t error_code:s",
)


def _user_name(col: Any) -> Any:
    return select(users.c.name).where(users.c.id == col).scalar_subquery()


def _q(stmt: Select[Any], f: P2ListFilters, *cols: Any) -> Select[Any]:
    term = (str(f.q) if f.q else "").strip()
    if not term:
        return stmt
    like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    return stmt.where(or_(*(c.ilike(like, escape="\\") for c in cols)))


# ================================================================ agent runs
async def _agent_runs(
    ctx: RequestContext,
    db: AsyncSession,
    principal: Principal,
    f: P2ListFilters,
    tz: ZoneInfo,
    avail: frozenset[str],
) -> Select[Any]:
    _need(avail, "agent_runs")
    stmt = select(R.c.id)
    if not principal.has("ai:govern", Scope.TENANT):
        stmt = stmt.where(false())
    stmt = _common(stmt, f, date_col=R.c.created_at, status_col=R.c.status)
    stmt = _q(stmt, f, R.c.agent_key)
    return lists._order(stmt, "agent_runs", f.sort, R.c.id)


RUN_COLUMNS = (
    Column("run", "Run", "uuid", R.c.id),
    Column("agent_key", "Agent", "enum", R.c.agent_key),
    Column("origin", "Origin", "enum", R.c.origin),
    Column("trigger_kind", "Trigger", "enum", R.c.trigger_kind),
    Column("status", "Status", "enum", R.c.status),
    Column("reason_code", "Reason", "enum", R.c.reason_code),
    Column("subject_type", "Subject type", "enum", R.c.subject_type),
    Column("subject_id", "Subject", "uuid", R.c.subject_id),
    Column("actor_label", "For", "string", _user_name(R.c.owner_user_id)),
    Column("step_count", "Steps", "int", R.c.step_count),
    Column("tool_call_count", "Tool calls", "int", R.c.tool_call_count),
    Column("input_tokens", "Input tokens", "int", R.c.input_tokens),
    Column("output_tokens", "Output tokens", "int", R.c.output_tokens),
    Column("cost_usd", "Cost (USD)", "decimal", R.c.cost_micros / 1_000_000.0),
    Column("tainted", "Tainted", "bool", R.c.tainted),
    Column("policy_violations", "Policy violations", "int", R.c.policy_violations),
    Column("created_at", "Created", "datetime", R.c.created_at),
    Column("started_at", "Started", "datetime", R.c.started_at),
    Column("finished_at", "Finished", "datetime", R.c.finished_at),
)


# ================================================================ agent proposals
def _visible(principal: Principal) -> Any:
    if principal.has("ai:govern", Scope.TENANT):
        return None
    if not principal.has("proposal:decide"):
        return false()
    queues = select(_roles.c.key).where(_roles.c.id.in_(list(principal.role_ids) or []))
    return or_(
        P.c.assignee_user_id == principal.id,
        P.c.owner_user_id == principal.id,
        P.c.route_role_key.in_(queues),
    )


async def _agent_proposals(
    ctx: RequestContext,
    db: AsyncSession,
    principal: Principal,
    f: P2ListFilters,
    tz: ZoneInfo,
    avail: frozenset[str],
) -> Select[Any]:
    _need(avail, "agent_proposals")
    stmt = select(P.c.id).where(P.c.origin == "agent")
    cond = _visible(principal)
    if cond is not None:
        stmt = stmt.where(cond)
    stmt = _common(stmt, f, date_col=P.c.created_at, status_col=P.c.status)
    stmt = _q(stmt, f, P.c.human_id, P.c.title)
    return lists._order(stmt, "agent_proposals", f.sort, P.c.id)


PROPOSAL_COLUMNS = (
    Column("human_id", "Proposal", "string", P.c.human_id),
    Column("agent_key", "Agent", "enum", P.c.agent_key),
    Column("tool_key", "Action", "enum", P.c.tool_key),
    Column("title", "Title", "string", P.c.title),
    Column("risk", "Risk", "enum", P.c.risk),
    Column("status", "Status", "enum", P.c.status),
    Column("edited", "Edited", "bool", P.c.edited),
    Column("target_type", "Target type", "enum", P.c.target_type),
    Column("target_id", "Target", "uuid", P.c.target_id),
    Column("assignee", "Assignee", "string", _user_name(P.c.assignee_user_id)),
    Column("route_role_key", "Queue", "enum", P.c.route_role_key),
    Column("owner", "Owner", "string", _user_name(P.c.owner_user_id)),
    Column("created_at", "Created", "datetime", P.c.created_at),
    Column("expires_at", "Expires", "datetime", P.c.expires_at),
    Column("decided_by", "Decided by", "string", _user_name(P.c.decided_by)),
    Column("decided_at", "Decided", "datetime", P.c.decided_at),
    Column("executed_at", "Executed", "datetime", P.c.executed_at),
    Column("error_code", "Error", "enum", P.c.error_code),
)

KINDS: tuple[ListKindDef, ...] = (
    ListKindDef("agent_runs", P2ListFilters, RUN_COLUMNS, _agent_runs, "ai:govern", R.c.id, "-created_at"),
    ListKindDef("agent_proposals", P2ListFilters, PROPOSAL_COLUMNS, _agent_proposals, "proposal:decide", P.c.id, "-created_at"),
)  # fmt: skip
P4_KINDS = tuple(k.kind for k in KINDS)

for _k in KINDS:
    register_list_kind(_k)
ALT_READ_PERMISSIONS["agent_proposals"] = ("ai:govern",)

__all__ = ["KINDS", "P4_KINDS", "PROPOSAL_COLUMNS", "RUN_COLUMNS"]
