"""``esign`` provider port (AD-29, Epic 22 A8, FR-121).

Owned by Epic 22 (R16). ``builtin`` (default) signs in-app (``documents.esign``); ``fake`` serves tests;
``manual`` asks a person. External adapters (later) run through ``connector_jobs`` with the idempotency key
``esign:create:signature_request:<id>:1`` and receive webhooks at ``/api/v1/hooks/esign/<config_id>``.
Consumers (onboarding, contracts, extensions) depend only on ``signature_requests.status`` and its events.
Every adapter also implements the generic ``ConnectorPort`` (``handle`` / ``verify_webhook`` / ``parse_webhook``).
"""

import dataclasses
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable


@dataclasses.dataclass(frozen=True, slots=True)
class EsignSignerDTO:
    order: int
    name: str
    email: str


@dataclasses.dataclass(frozen=True, slots=True)
class EsignRequestDTO:
    tenant_id: uuid.UUID
    request_id: uuid.UUID
    title: str
    doc_sha256: str
    pdf: bytes
    signers: tuple[EsignSignerDTO, ...]
    expires_at: Any


@dataclasses.dataclass(frozen=True, slots=True)
class EsignStatusDTO:
    status: str  # sent | partially_signed | completed | declined | expired | cancelled
    signed_orders: tuple[int, ...] = ()
    detail: Mapping[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True, slots=True)
class EsignWebhookDTO:
    external_ref: str
    status: EsignStatusDTO


@runtime_checkable
class EsignProvider(Protocol):
    key: str

    async def create_request(self, req: EsignRequestDTO) -> str: ...  # external_ref

    async def get_status(self, external_ref: str) -> EsignStatusDTO: ...

    async def fetch_signed(self, external_ref: str) -> bytes | None: ...

    async def handle_webhook(self, headers: Mapping[str, str], body: bytes) -> EsignWebhookDTO: ...


__all__ = [
    "Any",
    "EsignProvider",
    "EsignRequestDTO",
    "EsignSignerDTO",
    "EsignStatusDTO",
    "EsignWebhookDTO",
    "Mapping",
    "Sequence",
]
