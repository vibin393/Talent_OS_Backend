# ruff: noqa: E501
"""Darwinbox adapter (Epic 71, spec A3; key ``darwinbox``). Live mode only; tests use recorded fixtures.

Darwinbox exposes client-specific REST APIs enabled by the Darwinbox account team (spec Known gap 1): field ids are
the client's configured keys (custom keys are entered on the mapping page) and the endpoint paths are confirmed per
customer, so they are configuration (defaults below). Authentication: HTTP basic (``username`` / ``password``) plus
``api_key`` in the JSON body. ``push_exit`` without a configured separation path -> ``ManualActionRequired``.
"""

import base64
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.integrations.errors import ConnectorAuthError, ManualActionRequired
from app.services.connectors.hrms.keka import keka_status
from app.services.connectors.hrms.live_base import LiveHrmsBase, classify
from app.services.connectors.hrms.port import (
    BoardFieldDTO,
    ChangeDTO,
    FieldSchemaDTO,
    HireDTO,
    HrmsResultDTO,
    HrmsStatusDTO,
)

__all__ = ["CORE_FIELDS", "CUSTOM_KEY_RE", "EGRESS_HOSTS", "DarwinboxConfig", "DarwinboxHrms"]

EGRESS_HOSTS = ("*.darwinbox.in", "*.darwinbox.com")
CUSTOM_KEY_RE = r"^[A-Za-z0-9_]{1,100}$"
CORE_FIELDS: tuple[tuple[str, str, bool, str], ...] = (
    ("employee_id", "Employee ID", False, "text"),
    ("first_name", "First name", True, "text"),
    ("last_name", "Last name", True, "text"),
    ("email", "Work email", True, "text"),
    ("mobile", "Mobile", False, "text"),
    ("date_of_joining", "Date of joining", True, "date"),
    ("designation", "Designation", False, "text"),
    ("department", "Department", True, "text"),
    ("office_location", "Office location", False, "text"),
    ("employee_type", "Employee type", False, "text"),
    ("reporting_manager", "Reporting manager", False, "text"),
)


class DarwinboxConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subdomain: str = Field(default="", max_length=60, pattern=r"^([a-z0-9-]{2,60})?$")
    company_id: str | None = Field(default=None, max_length=60)
    add_path: str = Field(default="/importapi/addemployee", max_length=200, pattern=r"^/[A-Za-z0-9/_-]*$")
    update_path: str = Field(default="/importapi/updateemployee", max_length=200, pattern=r"^/[A-Za-z0-9/_-]*$")
    master_path: str = Field(default="/masterapi/employee", max_length=200, pattern=r"^/[A-Za-z0-9/_-]*$")
    separation_path: str | None = Field(default=None, max_length=200, pattern=r"^/[A-Za-z0-9/_-]*$")


class DarwinboxHrms(LiveHrmsBase):
    provider_key = "darwinbox"

    @property
    def _cfg(self) -> DarwinboxConfig:
        return self.ctx.config  # type: ignore[no-any-return]

    def _url(self, path: str) -> str:
        if not self._cfg.subdomain:
            msg = "The Darwinbox subdomain is not configured"
            raise ConnectorAuthError(msg)
        return f"https://{self._cfg.subdomain}.darwinbox.in{path}"

    def _headers(self) -> dict[str, str]:
        user, password = self.ctx.secrets.get("username"), self.ctx.secrets.get("password")
        if not user or not password or not self.ctx.secrets.get("api_key"):
            msg = "The Darwinbox API credentials are missing"
            raise ConnectorAuthError(msg)
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        return {"Authorization": f"Basic {token}", "Accept": "application/json"}

    def _body(self, extra: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {"api_key": self.ctx.secrets.get("api_key"), **extra}
        if self._cfg.company_id:
            body["company_id"] = self._cfg.company_id
        return body

    async def _post(self, path: str, what: str, payload: dict[str, Any]) -> Any:
        resp = await self.ctx.http.post(self._url(path), headers=self._headers(), json=self._body(payload))
        classify(resp, what)
        return resp.json() if resp.content else {}

    async def field_schema(self) -> FieldSchemaDTO:
        self._headers()  # credentials present (no call: the core set is static; custom keys come from the admin)
        return FieldSchemaDTO(fields=tuple(BoardFieldDTO(k, label, req, kind) for k, label, req, kind in CORE_FIELDS))  # type: ignore[arg-type]

    async def push_hire(self, hire: HireDTO) -> HrmsResultDTO:
        body = await self._post(self._cfg.add_path, "add employee", {"employee_data": [dict(hire.fields)]})
        data = body.get("data") if isinstance(body, dict) else None
        ref = (data or {}).get("employee_id") if isinstance(data, dict) else None
        ref = ref or (body.get("employee_id") if isinstance(body, dict) else None) or hire.fields.get("employee_id")
        if not ref:
            return HrmsResultDTO(employee_ref=None, status="pending", message="Darwinbox accepted the record; the employee id will arrive with the status pull")
        return HrmsResultDTO(employee_ref=str(ref), status="created", message=None)

    async def push_change(self, change: ChangeDTO) -> HrmsResultDTO:
        fields = {"employee_id": change.employee_ref, "effective_date": change.effective_date.isoformat(), **dict(change.fields)}
        await self._post(self._cfg.update_path, "update employee", {"employee_data": [fields]})
        return HrmsResultDTO(employee_ref=change.employee_ref, status="updated", message=None)

    async def push_exit(self, employee_ref: str, last_day: date) -> HrmsResultDTO:
        if not self._cfg.separation_path:
            raise ManualActionRequired("Record the exit in Darwinbox", {"op": "push_exit", "employee_ref": employee_ref, "last_day": last_day.isoformat()})
        await self._post(self._cfg.separation_path, "employee exit", {"employee_id": employee_ref, "date_of_exit": last_day.isoformat()})
        return HrmsResultDTO(employee_ref=employee_ref, status="updated", message=None)

    async def pull_status(self, since: datetime | None, cursor: str | None) -> tuple[Sequence[HrmsStatusDTO], str | None]:
        page = int(cursor) if cursor and cursor.isdigit() else 1
        payload: dict[str, Any] = {"page": page}
        if since is not None:
            payload["last_modified"] = since.date().isoformat()
        body = await self._post(self._cfg.master_path, "employee master", payload)
        items = body.get("employee_data") or body.get("data") if isinstance(body, dict) else body
        out: list[HrmsStatusDTO] = []
        for item in items or []:
            if not isinstance(item, dict) or not item.get("employee_id"):
                continue
            exit_raw = item.get("date_of_exit")
            exit_date = date.fromisoformat(str(exit_raw)[:10]) if exit_raw else None
            out.append(
                HrmsStatusDTO(
                    employee_ref=str(item["employee_id"]),
                    employee_code=str(item.get("employee_id"))[:60],
                    status="exited" if exit_date else keka_status(item.get("employee_status") or "active"),
                    exit_date=exit_date,
                )
            )
        more = bool(body.get("has_more")) if isinstance(body, dict) else False
        return out, (str(page + 1) if more else None)
