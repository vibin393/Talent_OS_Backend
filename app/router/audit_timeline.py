"""Record timeline (Epic 35 story 35.5, FR-183): ``GET /audit/timeline`` (audit:read or billing:read)."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.errors import Forbidden
from app.core.permissions import Principal, require_any
from app.dependencies.auth import TenantDBSessionDep
from app.services import audit_timeline_service

router = APIRouter(prefix="/audit", tags=["audit"])
# ``invoice:read`` (spec A6) does not exist in the catalogue; invoices are read with ``billing:read``.
TimelineRead = Annotated[Principal, Depends(require_any("audit:read", "billing:read"))]
# Without ``audit:read`` only billing documents' timelines are readable.
BILLING_SUBJECTS = frozenset({"invoice", "credit_note", "debit_note"})


class TimelineChangeOut(BaseModel):
    field: str
    old: Any = None
    new: Any = None
    masked: bool = False


class TimelineItemOut(BaseModel):
    id: uuid.UUID
    at: datetime
    action: str
    entity_type: str | None
    entity_id: str | None
    actor_type: str
    actor_id: uuid.UUID | None
    actor_display: str
    summary: str
    changes: list[TimelineChangeOut]


class TimelineOut(BaseModel):
    items: list[TimelineItemOut]
    next_cursor: str | None


@router.get("/timeline", response_model=TimelineOut)
async def get_timeline(
    principal: TimelineRead,
    db: TenantDBSessionDep,
    subject_id: uuid.UUID,
    subject_type: Annotated[
        str, Query(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    ] = "invoice",
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[
        int, Query(ge=1, le=audit_timeline_service.MAX_LIMIT)
    ] = audit_timeline_service.DEFAULT_LIMIT,
) -> dict[str, Any]:
    if subject_type not in BILLING_SUBJECTS and not principal.has("audit:read"):
        raise Forbidden(code="forbidden", details={"required_permission": "audit:read"})
    return await audit_timeline_service.timeline(db, subject_type, subject_id, cursor=cursor, limit=limit)
