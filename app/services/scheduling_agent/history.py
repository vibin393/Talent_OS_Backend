# ruff: noqa: E501
"""Scheduling request history and status changes (Epic 59 story 59.4: "the scheduling history shows each step").

``detail`` holds ids, times, counts and codes only (never notes or names)."""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import emit
from app.events.scheduling_agent import SchedulingRequestChanged
from app.models.scheduling_agent import SchedulingRequest, SchedulingRequestEvent
from app.services.scheduling_agent.common import now


def _clean(detail: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (detail or {}).items():
        if isinstance(v, uuid.UUID):
            out[k] = str(v)
        elif hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif isinstance(v, list | tuple):
            out[k] = [
                str(i) if isinstance(i, uuid.UUID) else (i.isoformat() if hasattr(i, "isoformat") else i)
                for i in v
            ]
        else:
            out[k] = v
    return out


def record(
    db: AsyncSession,
    req: SchedulingRequest,
    kind: str,
    *,
    actor_kind: str = "system",
    actor_id: uuid.UUID | None = None,
    detail: dict[str, Any] | None = None,
) -> SchedulingRequestEvent:
    row = SchedulingRequestEvent(
        id=uuid.uuid4(), tenant_id=req.tenant_id, request_id=req.id, kind=kind, actor_kind=actor_kind[:20],
        actor_id=actor_id, detail=_clean(detail), at=now(),
    )  # fmt: skip
    db.add(row)
    return row


def set_status(db: AsyncSession, req: SchedulingRequest, status: str) -> None:
    if req.status == status:
        return
    req.status = status
    emit(
        db,
        SchedulingRequestChanged(
            tenant_id=req.tenant_id, request_id=req.id, application_id=req.application_id, status=status,
            round=int(req.round), kind=req.kind, interview_id=req.booked_interview_id or req.interview_id,
        ),
    )  # fmt: skip


async def list_for(db: AsyncSession, request_id: uuid.UUID) -> list[SchedulingRequestEvent]:
    return list(
        (
            await db.execute(
                select(SchedulingRequestEvent)
                .where(SchedulingRequestEvent.request_id == request_id)
                .order_by(SchedulingRequestEvent.at, SchedulingRequestEvent.created_at)
            )
        ).scalars()
    )


__all__ = ["list_for", "record", "set_status"]
