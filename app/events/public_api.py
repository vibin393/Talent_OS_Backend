# ruff: file-ignore[line-too-long]
"""Public API events (Epic 67): the public bulk import lifecycle.

Contract layer (AD-36): frozen, slotted dataclasses; ``tenant_id`` first; ids and scalars only (no PII); dispatched
after commit (``PublicImportCompleted`` also feeds the ``bulk_import.completed`` public event in the transaction).
"""

import dataclasses
import uuid

__all__ = ["PublicApiDeprecatedUsage", "PublicImportCompleted", "PublicImportQueued"]


@dataclasses.dataclass(frozen=True, slots=True)
class PublicImportQueued:
    tenant_id: uuid.UUID
    import_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class PublicImportCompleted:
    tenant_id: uuid.UUID
    import_id: uuid.UUID
    resource: str
    status: str
    created: int
    updated: int
    duplicates: int
    failed: int


@dataclasses.dataclass(frozen=True, slots=True)
class PublicApiDeprecatedUsage:
    """The monthly report found deprecated calls of one Installation or API key (``api_deprecation_notices``)."""

    tenant_id: uuid.UUID
    notice_id: uuid.UUID
    installation_id: uuid.UUID | None
    api_key_id: uuid.UUID | None
    route_template: str
    call_count: int
    sunset: str
