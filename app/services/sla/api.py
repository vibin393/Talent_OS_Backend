"""Router-facing facade of the Epic 32 SLA administration (one service call per endpoint)."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.schemas.sla import EscalationWatchOut
from app.services.sla.clocks import list_clocks, summary
from app.services.sla.escalations import (
    archive_policy,
    create_policy,
    get_chain_escalations,
    get_policy,
    list_policies,
    list_watches,
    set_chain_escalations,
    update_policy,
)
from app.services.sla.targets import (
    create_target,
    delete_target,
    list_milestones,
    list_targets,
    update_target,
)

__all__ = [
    "archive_policy",
    "create_policy",
    "create_target",
    "delete_target",
    "get_chain_escalations",
    "get_policy",
    "list_clocks",
    "list_escalations",
    "list_milestones",
    "list_policies",
    "list_targets",
    "set_chain_escalations",
    "summary",
    "update_policy",
    "update_target",
]


async def list_escalations(
    ctx: RequestContext, db: AsyncSession, subject_kind: str, subject_id: uuid.UUID
) -> list[EscalationWatchOut]:
    """Watch history of an approval request or task; the caller must be able to read the subject (404)."""
    reader: Any
    if subject_kind == "approval_step":
        from app.services import approvals

        reader = approvals.get_request
    else:
        from app.services import tasks_service

        reader = tasks_service.get_task
    await reader(ctx, db, subject_id)
    return await list_watches(ctx, db, subject_kind, subject_id)
