"""Portal Copilot events (Epic 77 A7). Ids and scalars only; no conversation content. No Public Events
(conversations are private)."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class PortalCopilotDraftConfirmed:
    tenant_id: uuid.UUID
    proposal_id: uuid.UUID
    tool_key: str
    principal_type: str
    principal_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class PortalCopilotHandoff:
    tenant_id: uuid.UUID
    candidate_id: uuid.UUID
    application_id: uuid.UUID | None
    task_id: uuid.UUID


__all__ = ["PortalCopilotDraftConfirmed", "PortalCopilotHandoff"]
