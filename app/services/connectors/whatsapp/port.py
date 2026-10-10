# ruff: noqa: E501
"""``whatsapp`` provider port (AD-29, Epic 33 A4; owned by Epic 33 after the foundation, R16).

Template messages only (no free-form text). The config is bound by the adapter factory. Webhooks use the generic
port methods; ``parse_webhook`` returns ``{"events": [ChannelEvent as dict]}`` (see ``sms.port``).
"""

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from app.services.connectors.sms.port import ChannelEvent, PermanentSendError, SendResult, events_from_payload


@dataclasses.dataclass(frozen=True, slots=True)
class ProviderTemplate:
    name: str
    language: str
    status: str  # APPROVED | PENDING | REJECTED ...
    body: str
    variable_count: int = 0


@runtime_checkable
class WhatsAppProvider(Protocol):
    category: str
    provider_key: str

    async def send_template(
        self, to_e164: str, template_name: str, language: str, variables: Sequence[str]
    ) -> SendResult: ...

    async def list_templates(self) -> list[ProviderTemplate]: ...

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool: ...

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]: ...


__all__ = [
    "ChannelEvent",
    "PermanentSendError",
    "ProviderTemplate",
    "SendResult",
    "WhatsAppProvider",
    "events_from_payload",
]
