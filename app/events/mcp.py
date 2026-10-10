"""Governed MCP Server domain events (Epic 73, spec A5)."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class McpEnablementChanged:
    tenant_id: uuid.UUID
    on: bool
    tool_groups: tuple[str, ...]
    notice_version: str | None
    actor_id: uuid.UUID | None


@dataclasses.dataclass(frozen=True, slots=True)
class McpSessionOpened:
    tenant_id: uuid.UUID
    session_id: uuid.UUID
    run_id: uuid.UUID
    installation_id: uuid.UUID
    user_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class McpSessionEnded:
    tenant_id: uuid.UUID
    session_id: uuid.UUID
    run_id: uuid.UUID
    installation_id: uuid.UUID
    user_id: uuid.UUID
    reason: str


@dataclasses.dataclass(frozen=True, slots=True)
class McpSessionPaused:
    tenant_id: uuid.UUID
    session_id: uuid.UUID
    run_id: uuid.UUID
    user_id: uuid.UUID
    reason: str
    distinct_candidates: int


__all__ = ["McpEnablementChanged", "McpSessionEnded", "McpSessionOpened", "McpSessionPaused"]
