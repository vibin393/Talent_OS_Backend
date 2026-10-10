"""``assessment`` provider port (AD-29, Epic 36 contract 0.8; owned by Epic 40).

``manual`` (results are entered by a User in Epic 40) and ``fake`` (tests) ship with the contract; real
providers are added by Epic 40. Every adapter also implements the generic ``ConnectorPort`` (``handle`` /
``verify_webhook`` / ``parse_webhook``). Signatures are frozen.
"""

import dataclasses
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

__all__ = [
    "AssessmentProvider",
    "ProviderInviteDTO",
    "ProviderInviteResultDTO",
    "ProviderResultDTO",
    "ProviderTestDTO",
    "ProviderWebhookDTO",
]


@dataclasses.dataclass(frozen=True, slots=True)
class ProviderTestDTO:
    external_id: str
    name: str
    duration_minutes: int | None
    skill_tags: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True, slots=True)
class ProviderInviteDTO:
    tenant_id: uuid.UUID
    invitation_id: uuid.UUID
    external_test_id: str
    candidate_email: str
    candidate_name: str
    deadline_at: datetime | None
    callback_url: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class ProviderInviteResultDTO:
    external_ref: str
    take_url: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class ProviderResultDTO:
    status: str  # pending | completed | expired | cancelled
    score: Decimal | None  # 0..100
    section_scores: Mapping[str, Decimal] = dataclasses.field(default_factory=dict)
    skill_scores: Mapping[str, Decimal] = dataclasses.field(default_factory=dict)
    report_url: str | None = None
    completed_at: datetime | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class ProviderWebhookDTO:
    external_ref: str
    result: ProviderResultDTO | None


@runtime_checkable
class AssessmentProvider(Protocol):
    key: str

    async def list_tests(self) -> Sequence[ProviderTestDTO]: ...

    async def invite(self, req: ProviderInviteDTO) -> ProviderInviteResultDTO: ...

    async def fetch_result(self, external_ref: str) -> ProviderResultDTO | None: ...

    async def handle_webhook(self, headers: Mapping[str, str], body: bytes) -> ProviderWebhookDTO: ...
