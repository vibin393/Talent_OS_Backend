# ruff: file-ignore[line-too-long]
"""Erasure handler ``agent_runtime.runs`` (Epic 53, order 62; FR-326 / AD-66).

For a candidate subject: delete the encrypted payloads of runs whose subject is the candidate or whose
``candidate_ids`` contain it; null ``agent_tool_calls.args_redacted`` / ``result_summary`` of those runs and of tool
calls naming the candidate; blank Proposal ``args`` / ``preview`` / ``result`` / ``rationale`` / ``evidence`` where
``candidate_ids`` contains the candidate; clear the run ``result``. Counters, keys, statuses and tokens stay.
A top-level module (``load_erasure_handlers``), so privacy never imports the runtime.
"""

import uuid
from typing import Any

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_runtime import AgentPayload, AgentProposal, AgentRun, AgentToolCall
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler

ERASED_PREVIEW = {"title": "[erased]", "summary": "[erased]", "fields": [], "renderer": None}


async def _run_ids(db: AsyncSession, ids: list[uuid.UUID]) -> list[uuid.UUID]:
    rows = await db.execute(
        select(AgentRun.id).where(
            or_(
                (AgentRun.subject_type == "candidate") & AgentRun.subject_id.in_(ids),
                AgentRun.candidate_ids.overlap(ids),
            )
        )
    )
    return list(rows.scalars())


class AgentRunsHandler:
    name = "agent_runtime.runs"
    order = 62
    tables = ("agent_runs", "agent_run_steps", "agent_tool_calls", "agent_payloads", "agent_proposals")

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        if ctx.subject_kind != "candidate":
            return {"agent_runs": 0}
        ids = await subject_ids(db, ctx.candidate_id)
        run_ids = await _run_ids(db, ids)
        payloads = await db.execute(
            delete(AgentPayload).where(AgentPayload.run_id.in_(run_ids or [uuid.uuid4()]))
        )
        calls = await db.execute(
            update(AgentToolCall)
            .where(
                or_(
                    AgentToolCall.run_id.in_(run_ids or [uuid.uuid4()]),
                    AgentToolCall.candidate_ids.overlap(ids),
                )
            )
            .values(args_redacted=None, result_summary=None)
        )
        props = await db.execute(
            update(AgentProposal)
            .where(
                or_(
                    AgentProposal.candidate_ids.overlap(ids),
                    AgentProposal.run_id.in_(run_ids or [uuid.uuid4()]),
                )
            )
            .values(args={}, preview=ERASED_PREVIEW, result=None, rationale=None, evidence=[])
        )
        if run_ids:
            await db.execute(update(AgentRun).where(AgentRun.id.in_(run_ids)).values(result=None))
        await db.flush()
        return {
            "agent_runs": len(run_ids),
            "agent_payloads": int(getattr(payloads, "rowcount", 0) or 0),
            "agent_tool_calls": int(getattr(calls, "rowcount", 0) or 0),
            "agent_proposals": int(getattr(props, "rowcount", 0) or 0),
        }

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        if ctx.subject_kind != "candidate":
            return None
        run_ids = await _run_ids(db, [ctx.candidate_id])
        rows = (
            await db.execute(select(AgentRun).where(AgentRun.id.in_(run_ids or [uuid.uuid4()])))
        ).scalars()
        return {
            "agent_runs": [
                {"agent": r.agent_key, "status": r.status, "created_at": r.created_at, "result": r.result}
                for r in rows
            ]
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        if ctx.subject_kind != "candidate":
            return []
        ids = await subject_ids(db, ctx.candidate_id)
        run_ids = await _run_ids(db, ids)
        problems: list[str] = []
        if (
            run_ids
            and (
                await db.execute(select(AgentPayload.id).where(AgentPayload.run_id.in_(run_ids)).limit(1))
            ).first()
        ):
            problems.append("agent payloads remain for the candidate's runs")
        left = (
            await db.execute(
                select(AgentProposal.id)
                .where(AgentProposal.candidate_ids.overlap(ids), AgentProposal.rationale.is_not(None))
                .limit(1)
            )
        ).first()
        if left:
            problems.append("agent proposals keep a rationale about the candidate")
        return problems


register_erasure_handler(AgentRunsHandler())
