"""Workflow erasure handler ``workflow.waits`` (59), Epic 35 coverage gap for Epic 32 (AD-34).

``workflow_waits`` park a graph run on a delay / approval node for an entity. For an erased candidate the waits
on the candidate itself (and on the candidate's applications / placements) are cancelled and their ``extras``
(rendered node inputs, which may echo personal data) are cleared. The rows stay as bookkeeping (ids only).
"""

import uuid
from typing import Any

from sqlalchemy import Table, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import Base
from app.models.workflow import WorkflowWait
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler

# entity_type -> table holding ``candidate_id`` (resolved through metadata, no domain imports).
_CHILD_ENTITIES: dict[str, str] = {"application": "applications", "placement": "placements"}


async def _entity_ids(db: AsyncSession, candidate_id: uuid.UUID) -> dict[str, list[uuid.UUID]]:
    cids = await subject_ids(db, candidate_id)
    out: dict[str, list[uuid.UUID]] = {"candidate": list(cids)}
    for entity, table_name in _CHILD_ENTITIES.items():
        table: Table | None = Base.metadata.tables.get(table_name)
        if table is None or "candidate_id" not in table.c:
            continue
        out[entity] = list(
            (await db.execute(select(table.c.id).where(table.c.candidate_id.in_(cids)))).scalars()
        )
    return out


def _clause(ids: dict[str, list[uuid.UUID]]) -> Any:
    from sqlalchemy import and_, false, or_

    parts = [
        and_(WorkflowWait.entity_type == kind, WorkflowWait.entity_id.in_(v)) for kind, v in ids.items() if v
    ]
    return or_(*parts) if parts else false()


class WorkflowWaitsHandler:
    name = "workflow.waits"
    order = 59
    tables = ("workflow_waits",)

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await _entity_ids(db, ctx.candidate_id)
        retained = ctx.retained.get("workflow_waits", set())
        where = _clause(ids)
        if retained:
            where = where & WorkflowWait.id.not_in(retained)
        cancelled = await db.execute(
            update(WorkflowWait)
            .where(where, WorkflowWait.status == "waiting")
            .values(status="cancelled", outcome="erased")
            .execution_options(synchronize_session=False)
        )
        cleared = await db.execute(
            update(WorkflowWait)
            .where(where, WorkflowWait.extras != {})
            .values(extras={})
            .execution_options(synchronize_session=False)
        )
        await db.flush()
        return {"cancelled": int(cancelled.rowcount or 0), "cleared": int(cleared.rowcount or 0)}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:  # ruff: ignore[unused-method-argument]  # NOSONAR
        return None  # bookkeeping only

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await _entity_ids(db, ctx.candidate_id)
        where = _clause(ids)
        retained = ctx.retained.get("workflow_waits", set())
        if retained:
            where = where & WorkflowWait.id.not_in(retained)
        left = (
            await db.execute(
                select(WorkflowWait.id)
                .where(where, (WorkflowWait.status == "waiting") | (WorkflowWait.extras != {}))
                .limit(1)
            )
        ).first()
        return ["workflow wait with data remains"] if left else []


register_erasure_handler(WorkflowWaitsHandler())
