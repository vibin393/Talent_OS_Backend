"""``hrms`` port (Epic 66 contract, spec 0.9; Epic 71 fills real adapters, FR-365). Every adapter also implements the
generic ``ConnectorPort``. DTOs are frozen, slotted dataclasses; signatures are frozen. ``FieldSchemaDTO`` is shared
with the job-board port.
"""

import dataclasses
import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any, Literal, Protocol, runtime_checkable

from app.services.connectors.base import ConnectorPort
from app.services.connectors.job_board.port import BoardFieldDTO, FieldSchemaDTO

__all__ = [
    "BoardFieldDTO",
    "ChangeDTO",
    "FieldSchemaDTO",
    "HireDTO",
    "HrmsPort",
    "HrmsResultDTO",
    "HrmsStatusDTO",
]


@dataclasses.dataclass(frozen=True, slots=True)
class HireDTO:
    worker_id: uuid.UUID
    placement_id: uuid.UUID
    placement_version: int
    legal_entity_id: uuid.UUID
    fields: Mapping[str, Any]  # already mapped HRMS field -> value

    def __repr__(self) -> str:  # field values may be personal data
        return f"HireDTO(placement_id={self.placement_id}, placement_version={self.placement_version})"


@dataclasses.dataclass(frozen=True, slots=True)
class ChangeDTO:
    worker_id: uuid.UUID
    placement_id: uuid.UUID
    placement_version: int
    employee_ref: str
    fields: Mapping[str, Any]
    effective_date: date

    def __repr__(self) -> str:
        return f"ChangeDTO(placement_id={self.placement_id}, employee_ref={self.employee_ref!r})"


@dataclasses.dataclass(frozen=True, slots=True)
class HrmsResultDTO:
    employee_ref: str | None
    status: Literal["created", "updated", "pending", "failed"]
    message: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class HrmsStatusDTO:
    employee_ref: str
    employee_code: str | None
    status: Literal["active", "exited", "pending", "unknown"]
    exit_date: date | None


@runtime_checkable
class HrmsPort(ConnectorPort, Protocol):
    async def push_hire(self, hire: HireDTO) -> HrmsResultDTO: ...

    async def push_change(self, change: ChangeDTO) -> HrmsResultDTO: ...

    async def push_exit(self, employee_ref: str, last_day: date) -> HrmsResultDTO: ...

    async def pull_status(
        self, since: datetime | None, cursor: str | None
    ) -> tuple[Sequence[HrmsStatusDTO], str | None]: ...

    async def field_schema(self) -> FieldSchemaDTO: ...
