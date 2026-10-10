"""Shared helpers of the Epic 56 Sourcing Agent tools (``owner_epic="56"``, ``version=1``)."""

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.services import agent_runtime_service as runtime

OWNER_EPIC = "56"
VERSION = 1


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


def scoped(tctx: Any) -> Any:
    """Data-scope context: Effective Rights presented as the Accountable Owner (never used for writes)."""
    return runtime.scope_context(tctx.request, tctx.owner_user_id)


def subject_demand(tctx: Any) -> uuid.UUID | None:
    subject = tctx.subject
    return subject[1] if subject is not None and subject[0] == "demand" else None


__all__ = ["OWNER_EPIC", "VERSION", "In", "Out", "scoped", "subject_demand"]
