# ruff: noqa: E501
"""Governed MCP Server administration DTOs (Epic 73, spec A3 / A4). The protocol endpoint speaks JSON-RPC (no DTOs)."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

LowToolMode = Literal["suggest", "auto"]
ExecutionClass = Literal["read", "draft", "propose"]


class McpEnableIn(BaseModel):
    notice_version: str = Field(min_length=1, max_length=40)
    acknowledged: bool
    tool_groups: list[str] = Field(min_length=1, max_length=20)


class McpDisableIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class McpSettingsIn(BaseModel):
    tool_groups: list[str] | None = Field(default=None, max_length=20)
    calls_per_minute: int | None = Field(default=None, ge=5, le=120)
    clear_calls_per_minute: bool = False
    calls_per_user_day: int | None = Field(default=None, ge=100, le=2000)
    proposals_per_hour: int | None = Field(default=None, ge=1, le=25)
    bulk_read_threshold: int | None = Field(default=None, ge=50, le=500)
    max_results: int | None = Field(default=None, ge=10, le=50)
    allowed_origins: list[str] | None = Field(default=None, max_length=10)
    notify_user_on_first_connection: bool | None = None
    low_tool_modes: dict[str, LowToolMode] | None = None

    @field_validator("allowed_origins")
    @classmethod
    def _origins(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        import re

        pattern = re.compile(r"^https://[A-Za-z0-9.\-]+(:[0-9]{1,5})?$")
        out: list[str] = []
        for origin in value:
            origin = origin.strip().rstrip("/")
            if not pattern.fullmatch(origin):
                msg = f"Origins must look like https://host[:port] ({origin!r})"
                raise ValueError(msg)
            out.append(origin.lower())
        return sorted(set(out))


class McpToolOut(BaseModel):
    key: str
    name: str
    title: str
    group: str
    execution: ExecutionClass
    mcp_scope: str
    risk: str


class McpGroupOut(BaseModel):
    key: str
    label: str
    description: str
    enabled: bool
    tools: list[McpToolOut]


class McpSettingsOut(BaseModel):
    enabled: bool
    enabled_at: datetime | None = None
    enabled_by: uuid.UUID | None = None
    enabled_by_name: str | None = None
    notice_version: str | None = None
    current_notice_version: str
    notice_text: str
    agentic_enabled: bool
    kill_switch: bool
    tool_groups: list[str]
    groups: list[McpGroupOut]
    calls_per_minute: int | None = None
    tier_calls_per_minute: int
    calls_per_user_day: int
    proposals_per_hour: int
    bulk_read_threshold: int
    max_results: int
    allowed_origins: list[str]
    notify_user_on_first_connection: bool
    low_tool_modes: dict[str, LowToolMode]
    server_url: str


class McpSessionQuery(BaseModel):
    status: Literal["active", "paused", "ended"] | None = None
    user_id: uuid.UUID | None = None
    installation_id: uuid.UUID | None = None
    started_from: datetime | None = None
    started_to: datetime | None = None
    mine: bool = False
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class McpSessionOut(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    installation_id: uuid.UUID
    user_id: uuid.UUID
    user_name: str | None = None
    client_name: str
    client_version: str | None = None
    protocol_version: str
    status: str
    end_reason: str | None = None
    started_at: datetime
    last_seen_at: datetime
    ended_at: datetime | None = None
    call_count: int
    proposal_count: int
    refusal_count: int
    violation_count: int
    redaction_count: int
    distinct_candidates: int


class McpSessionPage(BaseModel):
    items: list[McpSessionOut]
    total: int
    page: int
    page_size: int


class McpEndSessionIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class McpHoldOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    user_name: str | None = None
    installation_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    reason: str
    held_at: datetime
    auto_release_at: datetime | None = None
    released_at: datetime | None = None
    note: str | None = None


class McpHoldReleaseIn(BaseModel):
    note: str = Field(min_length=1, max_length=500)


class McpMeOut(BaseModel):
    """``GET /mcp/me``: the self-service page state (My AI assistants)."""

    enabled: bool
    has_permission: bool
    on_hold: bool
    server_url: str


__all__ = [
    "ExecutionClass",
    "LowToolMode",
    "McpDisableIn",
    "McpEnableIn",
    "McpEndSessionIn",
    "McpGroupOut",
    "McpHoldOut",
    "McpHoldReleaseIn",
    "McpMeOut",
    "McpSessionOut",
    "McpSessionPage",
    "McpSessionQuery",
    "McpSettingsIn",
    "McpSettingsOut",
    "McpToolOut",
]
