# ruff: file-ignore[line-too-long]
"""``bgv`` provider port (AD-29, Epic 20 A5). Owned by Epic 20 after the foundation (R16).

Every adapter also implements the generic ``ConnectorPort`` (``handle`` / ``verify_webhook`` / ``parse_webhook``).
``BgvCheckJob`` is the **only** data a provider receives (FR-112 data minimisation): the check type, the subject
label and the file ids / declarations of the check's own document requests -- never the candidate's contact
details, other checks or internal notes.
"""

import dataclasses
import uuid
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, Literal, Protocol, runtime_checkable

BgvResult = Literal["verified", "discrepancy", "unable_to_verify"]


@dataclasses.dataclass(frozen=True)
class BgvCheckJob:
    check_id: uuid.UUID
    check_type: str
    subject_label: str
    file_ids: tuple[uuid.UUID, ...] = ()
    declarations: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "check_id": str(self.check_id),
            "check_type": self.check_type,
            "subject_label": self.subject_label,
            "file_ids": [str(f) for f in self.file_ids],
            "declarations": dict(self.declarations),
        }


@dataclasses.dataclass(frozen=True)
class BgvStatusDTO:
    external_ref: str
    status: Literal["pending", "in_progress", "completed", "cancelled"]


@dataclasses.dataclass(frozen=True)
class BgvReportDTO:
    external_ref: str
    result: BgvResult
    note: str | None = None
    verifier_name: str | None = None
    source_contacted: str | None = None
    verified_on: date | None = None


@dataclasses.dataclass(frozen=True)
class BgvWebhookDTO:
    external_ref: str
    report: BgvReportDTO | None = None


@runtime_checkable
class BgvProvider(Protocol):
    key: str

    async def initiate_check(self, job: BgvCheckJob) -> str: ...

    async def get_status(self, external_ref: str) -> BgvStatusDTO: ...

    async def fetch_report(self, external_ref: str) -> BgvReportDTO | None: ...

    async def handle_webhook(self, headers: Mapping[str, str], body: bytes) -> BgvWebhookDTO: ...


__all__ = [
    "BgvCheckJob",
    "BgvProvider",
    "BgvReportDTO",
    "BgvResult",
    "BgvStatusDTO",
    "BgvWebhookDTO",
    "Mapping",
    "Sequence",
]
