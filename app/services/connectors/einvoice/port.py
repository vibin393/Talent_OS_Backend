"""``einvoice`` provider port (AD-29): the methods of Epic 29 A9.

Owned by Epic 29 after the foundation (R16). Every adapter also implements the generic
``ConnectorPort`` (``handle`` / ``verify_webhook`` / ``parse_webhook``).
"""

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EInvoiceProvider(Protocol):
    key: str

    async def generate_irn(self, doc_payload: Mapping[str, Any]) -> Mapping[str, Any]: ...

    async def cancel_irn(self, irn: str, reason: str) -> Mapping[str, Any]: ...

    async def get_irn(self, doc_ref: str) -> Mapping[str, Any] | None: ...


__all__ = ["Any", "EInvoiceProvider", "Mapping", "Sequence"]
