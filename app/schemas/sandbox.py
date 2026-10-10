"""Sandbox Tenant schemas (Epic 66, Story 66.9, FR-339)."""

import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints

from app.schemas.identity import TenantOut


class SandboxCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    owner_kind: Literal["partner_org", "tenant", "platform"] = "platform"
    owner_id: uuid.UUID | None = None
    size: Literal["small", "medium"] = "small"
    seed: Annotated[int, Field(ge=0, le=2**31 - 1)] = 0
    # First-admin invitation (the developer). Default ``sandbox-admin@example.com`` (reserved, never delivered).
    admin_email: Annotated[EmailStr, StringConstraints(max_length=254)] | None = None


class SandboxSeedOut(BaseModel):
    size: Literal["small", "medium"]
    seed: int
    counts: dict[str, int]
    plan_digest: str


class SandboxOut(BaseModel):
    tenant: TenantOut
    mode: Literal["sandbox"]
    owner_kind: Literal["partner_org", "tenant", "platform"]
    owner_id: uuid.UUID | None
    seed: SandboxSeedOut
