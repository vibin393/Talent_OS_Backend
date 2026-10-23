"""Talent pool DTOs (Epic 38; Epic 36 contract, AD-39). Frozen; changing a field needs a spine amendment."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

__all__ = ["PoolSummary"]


class PoolSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    name: str
    kind: str
    segment_id: uuid.UUID | None
    owner_id: uuid.UUID | None
    visibility: str
    status: str
    member_count: int
    last_refreshed_at: datetime | None


# ---------------------------------------------------------------- Epic 38 API DTOs (not part of the contract)
from decimal import Decimal  # ruff: ignore[module-import-not-at-top-of-file]
from typing import Annotated, Any, Literal  # ruff: ignore[module-import-not-at-top-of-file]

from pydantic import (  # ruff: ignore[module-import-not-at-top-of-file]
    Field,
    StringConstraints,
    field_serializer,
)

Visibility = Literal["private", "team", "tenant"]
PoolName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InlineSegmentIn(_In):
    source_key: str = Field(max_length=60)
    definition: dict[str, Any]


class PoolCreate(_In):
    name: PoolName
    description: Description | None = None
    kind: Literal["static", "dynamic"] = "static"
    visibility: Visibility = "private"
    team_id: uuid.UUID | None = None
    segment_id: uuid.UUID | None = None
    segment: InlineSegmentIn | None = None


class PoolUpdate(_In):
    name: PoolName | None = None
    description: Description | None = None
    visibility: Visibility | None = None
    team_id: uuid.UUID | None = None


class PoolDiff(BaseModel):
    added: int = 0
    removed: int = 0


class PoolOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    kind: str
    segment_id: uuid.UUID | None
    segment_name: str | None
    source_key: str | None
    owner_id: uuid.UUID
    owner_name: str | None
    visibility: str
    team_id: uuid.UUID | None
    status: str
    archived_at: datetime | None
    template_key: str | None
    legacy_list_id: uuid.UUID | None
    member_count: int
    last_refreshed_at: datetime | None
    last_diff: PoolDiff | None
    needs_repair: bool
    subscribed: bool
    can_edit: bool
    created_at: datetime
    updated_at: datetime


class PoolMembersIn(_In):
    candidate_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)


class PoolMembersAddOut(BaseModel):
    added: int
    existing: int
    member_count: int


class PoolMemberOut(BaseModel):
    candidate_id: uuid.UUID
    full_name: str | None
    current_title: str | None
    source: str
    reason: dict[str, Any]
    added_at: datetime
    added_by: uuid.UUID | None
    consent_marketing: str
    consent_engagement: str


class PoolMembersPage(BaseModel):
    items: list[PoolMemberOut]
    total: int
    page: int
    page_size: int


class RefreshOut(BaseModel):
    id: uuid.UUID
    pool_id: uuid.UUID
    trigger: str
    status: str
    added: int
    removed: int
    evaluated: int
    duration_ms: int | None
    error_code: str | None
    started_at: datetime
    finished_at: datetime | None


class SubscriptionOut(BaseModel):
    subscribed: bool


class TemplateOut(BaseModel):
    key: str
    name: str
    description: str
    source_key: str
    enabled: bool
    available: bool
    unavailable_reason: str | None
    pool_id: uuid.UUID | None


class TemplateEnableIn(_In):
    batch: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)] | None = None


class PoolPinIn(_In):
    pool_id: uuid.UUID


class PoolPinOut(BaseModel):
    pool_id: uuid.UUID
    name: str
    kind: str
    member_count: int
    pinned_by: uuid.UUID
    pinned_at: datetime


class AddToPipelineIn(_In):
    candidate_ids: list[uuid.UUID] | None = Field(default=None, min_length=1, max_length=200)
    top_n: int | None = Field(default=None, ge=1, le=200)


class RediscoveryMemberOut(BaseModel):
    candidate_id: uuid.UUID
    full_name: str | None
    overall: Decimal
    rank: int

    @field_serializer("overall")
    def _overall(self, value: Decimal) -> str:
        return str(value)


class RediscoveryPoolOut(BaseModel):
    pool_id: uuid.UUID
    name: str
    kind: str
    pinned: bool
    matching_count: int
    top: list[RediscoveryMemberOut]


class PoolsSectionOut(BaseModel):
    matching_pools: int
    summary: str
    pools: list[RediscoveryPoolOut]


class PoolSettingsOut(BaseModel):
    nightly_refresh_local_time: str
    digest_local_time: str
    max_dynamic_pools: int


class PoolSettingsIn(_In):
    nightly_refresh_local_time: (
        Annotated[str, StringConstraints(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")] | None
    ) = None
    digest_local_time: (
        Annotated[str, StringConstraints(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")] | None
    ) = None
    max_dynamic_pools: int | None = Field(default=None, ge=1, le=100000)


__all__ += [
    "AddToPipelineIn",
    "InlineSegmentIn",
    "PoolCreate",
    "PoolDiff",
    "PoolMemberOut",
    "PoolMembersAddOut",
    "PoolMembersIn",
    "PoolMembersPage",
    "PoolOut",
    "PoolPinIn",
    "PoolPinOut",
    "PoolSettingsIn",
    "PoolSettingsOut",
    "PoolUpdate",
    "PoolsSectionOut",
    "RediscoveryMemberOut",
    "RediscoveryPoolOut",
    "RefreshOut",
    "SubscriptionOut",
    "TemplateEnableIn",
    "TemplateOut",
]
