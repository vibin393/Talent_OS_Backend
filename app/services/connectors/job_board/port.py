# ruff: noqa: E501
"""``job_board`` port (Epic 66 contract, spec 0.9; Epic 70 fills real adapters, FR-360). Every adapter also implements
the generic ``ConnectorPort``. DTOs are frozen, slotted dataclasses; signatures are frozen.
"""

import dataclasses
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Protocol, runtime_checkable

from app.services.connectors.base import ConnectorPort

__all__ = [
    "BoardApplicantDTO",
    "BoardFieldDTO",
    "BoardRefDTO",
    "BoardStatus",
    "FieldSchemaDTO",
    "JobBoardPort",
    "JobPostingDTO",
    "LocationDTO",
    "SalaryDTO",
]

BoardStatus = Literal["pending", "live", "expired", "closed", "rejected", "failed"]


@dataclasses.dataclass(frozen=True, slots=True)
class LocationDTO:
    city: str | None
    region: str | None
    country: str  # ISO-3166 alpha-2
    postal_code: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class SalaryDTO:
    currency: str
    min_amount: Decimal | None
    max_amount: Decimal | None
    period: Literal["hour", "day", "month", "year"] = "year"


@dataclasses.dataclass(frozen=True, slots=True)
class JobPostingDTO:
    posting_id: uuid.UUID
    demand_id: uuid.UUID
    revision: int
    title: str
    description_html: str  # sanitised Approved Content
    location: LocationDTO | None
    work_mode: Literal["onsite", "hybrid", "remote"]
    employment_type: str
    experience_min_years: Decimal | None
    experience_max_years: Decimal | None
    salary: SalaryDTO | None  # only when the posting's show_rate
    skills: tuple[str, ...]
    openings: int
    company_name: str | None  # None when not show_client_name
    apply_url: str  # careers apply URL with src=<board key>
    expires_at: datetime | None
    board_fields: Mapping[str, Any]  # mapped taxonomy values


@dataclasses.dataclass(frozen=True, slots=True)
class BoardRefDTO:
    board_ref: str
    url: str | None
    status: BoardStatus
    message: str | None
    expires_at: datetime | None
    views: int | None
    applies: int | None
    credits_left: int | None


@dataclasses.dataclass(frozen=True, slots=True)
class BoardApplicantDTO:
    board_application_id: str
    board_ref: str
    applied_at: datetime
    full_name: str
    email: str | None
    phone_e164: str | None
    resume: bytes | None
    resume_filename: str | None
    resume_media_type: str | None
    answers: tuple[tuple[str, str], ...]
    location: str | None
    current_title: str | None
    experience_years: Decimal | None

    def __repr__(self) -> str:  # no PII in logs
        return f"BoardApplicantDTO(board_application_id={self.board_application_id!r}, board_ref={self.board_ref!r})"


@dataclasses.dataclass(frozen=True, slots=True)
class BoardFieldDTO:
    key: str
    label: str
    required: bool
    kind: Literal["text", "number", "select", "multiselect", "date", "boolean"]
    options: tuple[tuple[str, str], ...] = ()  # (value, label) taxonomy


@dataclasses.dataclass(frozen=True, slots=True)
class FieldSchemaDTO:
    fields: tuple[BoardFieldDTO, ...]


@runtime_checkable
class JobBoardPort(ConnectorPort, Protocol):
    async def publish(self, posting: JobPostingDTO) -> BoardRefDTO: ...

    async def update(self, board_ref: str, posting: JobPostingDTO) -> BoardRefDTO: ...

    async def close(self, board_ref: str) -> BoardRefDTO: ...

    async def status(self, board_ref: str) -> BoardRefDTO: ...

    async def fetch_applicants(
        self, since: datetime | None, cursor: str | None
    ) -> tuple[Sequence[BoardApplicantDTO], str | None]: ...

    async def field_schema(self) -> FieldSchemaDTO: ...
