"""Manual ``hrms`` adapter (Epic 66, spec 0.9): pushes raise ``ManualActionRequired`` carrying one CSV row in the HRMS
import template (header = the mapped HRMS field keys), for a person to import into the HRMS."""

import csv
import io
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from app.core.integrations.context import AdapterContext
from app.core.integrations.errors import ManualActionRequired
from app.services.connectors.hrms.port import ChangeDTO, FieldSchemaDTO, HireDTO, HrmsResultDTO, HrmsStatusDTO

__all__ = ["ManualHrms", "import_csv"]


def import_csv(action: str, fields: Mapping[str, Any]) -> str:
    """One-row CSV: ``action`` + the mapped fields (sorted header, deterministic)."""
    header = ["action", *sorted(fields)]
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerow([action, *("" if fields[k] is None else str(fields[k]) for k in sorted(fields))])
    return buf.getvalue()


class ManualHrms:
    category = "hrms"
    provider_key = "manual"
    key = "manual"

    def __init__(self, ctx: AdapterContext | None = None) -> None:
        self.ctx = ctx

    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        raise ManualActionRequired(f"hrms: complete {op} manually", {"op": op})

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:  # ruff: ignore[unused-method-argument]
        return False

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        return {}

    async def push_hire(self, hire: HireDTO) -> HrmsResultDTO:
        raise ManualActionRequired(
            "Import this hire into the HRMS using the CSV, then record the employee reference",
            {"op": "push_hire", "placement_id": str(hire.placement_id), "csv": import_csv("hire", hire.fields)},
        )

    async def push_change(self, change: ChangeDTO) -> HrmsResultDTO:
        fields = {"employee_ref": change.employee_ref, "effective_date": change.effective_date.isoformat(), **change.fields}
        raise ManualActionRequired(
            "Import this change into the HRMS using the CSV",
            {"op": "push_change", "employee_ref": change.employee_ref, "csv": import_csv("change", fields)},
        )

    async def push_exit(self, employee_ref: str, last_day: date) -> HrmsResultDTO:
        fields = {"employee_ref": employee_ref, "last_day": last_day.isoformat()}
        raise ManualActionRequired(
            "Record this exit in the HRMS using the CSV",
            {"op": "push_exit", "employee_ref": employee_ref, "csv": import_csv("exit", fields)},
        )

    async def pull_status(
        self,
        since: datetime | None,  # ruff: ignore[unused-method-argument]
        cursor: str | None,  # ruff: ignore[unused-method-argument]
    ) -> tuple[Sequence[HrmsStatusDTO], str | None]:
        return (), None

    async def field_schema(self) -> FieldSchemaDTO:
        return FieldSchemaDTO(fields=())
