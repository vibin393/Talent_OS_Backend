"""``payroll`` provider port (AD-29): the methods of Epic 28 A7 (``ExportPayload`` / ``ExternalRef`` / ``ProviderStatus`` are Epic 28's).

Owned by Epic 28 after the foundation (R16). Every adapter also implements the generic
``ConnectorPort`` (``handle`` / ``verify_webhook`` / ``parse_webhook``).
"""

import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PayrollProvider(Protocol):
    key: str

    async def get_status(self, external_ref: str) -> Any: ...


@runtime_checkable
class PayrollPushPort(Protocol):
    """Epic 66 (spec 0.9; Epic 71 fills real adapters): push a payroll export file. Supersedes the unused Phase 2
    ``PayrollProvider.push_export(version, file_ref) -> str`` (no caller existed)."""

    async def push_export(self, export_id: uuid.UUID, file: bytes, media_type: str) -> Mapping[str, Any]: ...


__all__ = ["Any", "Mapping", "PayrollProvider", "PayrollPushPort", "Sequence"]
