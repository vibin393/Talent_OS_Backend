# ruff: noqa: E501
"""Shared helpers of the Epic 59 scheduling tools (``agent_tools.scheduling`` and ``agent_tools.interviews``).

Every request-scoped tool takes ``request_id``; the request must belong to the run's subject (the Application), else
``not_found`` (scope ``cross_subject=True`` + this binding, as the request id is not a related ref of the run).
Approved Proposals run with the approver's context and the run's subject."""

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.core.errors import NotFound
from app.services import scheduling_agent_service as svc

OWNER_EPIC = "59"
VERSION = 1


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


def subject_application(tctx: Any) -> uuid.UUID | None:
    subject = getattr(tctx, "subject", None)
    if subject is not None and subject[0] == "application":
        return subject[1]
    return None


async def load_request(tctx: Any, request_id: uuid.UUID, *, for_update: bool = False) -> Any:
    from app.services.scheduling_agent.common import request_row

    try:
        req = await request_row(tctx.db, request_id, for_update=for_update)
    except NotFound:
        raise NotFound("Scheduling request not found") from None
    app_id = subject_application(tctx)
    if app_id is None or req.application_id != app_id:
        raise NotFound("Scheduling request not found")
    return req


def refs_of(req: Any, *extra: tuple[str, uuid.UUID]) -> tuple[tuple[str, uuid.UUID], ...]:
    return (("scheduling_request", req.id), ("candidate", req.candidate_id), *extra)


__all__ = ["OWNER_EPIC", "VERSION", "In", "Out", "load_request", "refs_of", "subject_application", "svc"]
