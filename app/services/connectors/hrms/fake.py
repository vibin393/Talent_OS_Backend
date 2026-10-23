"""Fake ``hrms`` adapter (Epic 66, spec 0.9): assigns employee codes ``FK-<n>`` (idempotent per placement),
scripted exits (``script["exits"] = {"FK-1": "2026-03-31"}``), a small field schema. No network."""

import uuid
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, ClassVar

from app.core.integrations.errors import ConnectorPermanentError
from app.core.integrations.fakes import FakeAdapterBase
from app.services.connectors.hrms.port import (
    BoardFieldDTO,
    ChangeDTO,
    FieldSchemaDTO,
    HireDTO,
    HrmsResultDTO,
    HrmsStatusDTO,
)

__all__ = ["FakeHrms"]

_FIELDS = FieldSchemaDTO(
    fields=(
        BoardFieldDTO("first_name", "First name", True, "text"),
        BoardFieldDTO("last_name", "Last name", True, "text"),
        BoardFieldDTO("date_of_joining", "Date of joining", True, "date"),
        BoardFieldDTO(
            "department",
            "Department",
            False,
            "select",
            (("engineering", "Engineering"), ("operations", "Operations"), ("finance", "Finance")),
        ),
    )
)


class FakeHrms(FakeAdapterBase):
    category = "hrms"
    provider_key = "fake_hrms"
    _seq: ClassVar[int] = 0
    _by_placement: ClassVar[dict[uuid.UUID, str]] = {}
    _employees: ClassVar[dict[str, dict[str, Any]]] = {}

    @classmethod
    def reset(cls) -> None:
        super().reset()
        cls._seq = 0
        cls._by_placement.clear()
        cls._employees.clear()

    def _employee(self, ref: str) -> dict[str, Any]:
        emp = type(self)._employees.get(ref)
        if emp is None:
            msg = "Unknown employee reference"
            raise ConnectorPermanentError("employee_unknown", msg)
        return emp

    async def push_hire(self, hire: HireDTO) -> HrmsResultDTO:
        self._check("push_hire", hire.placement_id)
        cls = type(self)
        ref = cls._by_placement.get(hire.placement_id)
        if ref is None:
            cls._seq += 1
            ref = f"FK-{cls._seq}"
            cls._by_placement[hire.placement_id] = ref
            cls._employees[ref] = {"status": "active", "exit_date": None, "version": hire.placement_version}
        return HrmsResultDTO(employee_ref=ref, status="created", message=None)

    async def push_change(self, change: ChangeDTO) -> HrmsResultDTO:
        self._check("push_change", change.employee_ref)
        emp = self._employee(change.employee_ref)
        emp["version"] = max(emp["version"], change.placement_version)
        return HrmsResultDTO(employee_ref=change.employee_ref, status="updated", message=None)

    async def push_exit(self, employee_ref: str, last_day: date) -> HrmsResultDTO:
        self._check("push_exit", employee_ref)
        emp = self._employee(employee_ref)
        emp["status"] = "exited"
        emp["exit_date"] = last_day
        return HrmsResultDTO(employee_ref=employee_ref, status="updated", message=None)

    async def pull_status(
        self,
        since: datetime | None,  # ruff: ignore[unused-method-argument]
        cursor: str | None,
    ) -> tuple[Sequence[HrmsStatusDTO], str | None]:
        self._check("pull_status", cursor)
        exits = {k: date.fromisoformat(str(v)) for k, v in (self.scripted().get("exits") or {}).items()}
        items: list[HrmsStatusDTO] = []
        for ref, emp in sorted(type(self)._employees.items(), key=lambda kv: int(kv[0].split("-")[1])):
            exit_date = exits.get(ref, emp["exit_date"])
            status = "exited" if exit_date is not None else emp["status"]
            items.append(HrmsStatusDTO(employee_ref=ref, employee_code=ref, status=status, exit_date=exit_date))  # type: ignore[arg-type]
        start = int(cursor) if cursor and cursor.isdigit() else 0
        size = 50
        page = items[start : start + size]
        return page, (str(start + size) if start + size < len(items) else None)

    async def field_schema(self) -> FieldSchemaDTO:
        self._check("field_schema")
        return _FIELDS
