# ruff: file-ignore[line-too-long]
"""Keka HR adapter (Epic 71, spec A3; key ``keka``). Live mode only; tests use recorded fixtures (``httpx.MockTransport``).

API (Keka public API v1, enabled per customer; confirmed with the vendor during onboarding -- spec Known gap 1):
* token: ``POST https://login.keka.com/connect/token`` (form ``grant_type=kekaapi``, ``scope=kekaapi``, ``client_id``,
  ``client_secret``, ``api_key``) -> bearer ``access_token`` (cached per adapter instance until ``expires_in``);
* base ``https://{company_slug}.{environment}.com/api/v1``;
* ``field_schema``: the documented employee fields (static) with lookups from ``/hris/departments``, ``/hris/locations``
  and ``/hris/jobtitles`` when available;
* ``push_hire`` -> ``POST /hris/employees`` (``data.id`` -> employee ref, ``employeeNumber`` -> code);
* ``push_change`` -> ``PUT /hris/employees/{id}`` (changed fields only);
* ``push_exit`` -> ``POST /hris/employees/{id}/exit`` with ``lastWorkingDate``; 404/405 (not on the plan) ->
  ``ManualActionRequired`` "Record the exit in Keka";
* ``pull_status(since, cursor)`` -> ``GET /hris/employees?lastModified=<iso>&pageNumber=<n>&pageSize=100``.
"""

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.integrations.errors import ConnectorAuthError, ManualActionRequired
from app.services.connectors.hrms.live_base import LiveHrmsBase, classify
from app.services.connectors.hrms.port import (
    BoardFieldDTO,
    ChangeDTO,
    FieldSchemaDTO,
    HireDTO,
    HrmsResultDTO,
    HrmsStatusDTO,
)

__all__ = ["EGRESS_HOSTS", "TOKEN_URL", "KekaConfig", "KekaHrms", "keka_status"]

TOKEN_URL = "https://login.keka.com/connect/token"
EGRESS_HOSTS = ("*.keka.com", "login.keka.com", "*.kekademo.com")
PAGE_SIZE = 100

_FIELDS: tuple[tuple[str, str, bool, str], ...] = (
    ("firstName", "First name", True, "text"),
    ("middleName", "Middle name", False, "text"),
    ("lastName", "Last name", True, "text"),
    ("email", "Work email", True, "text"),
    ("mobilePhone", "Mobile phone", False, "text"),
    ("dateOfJoining", "Date of joining", True, "date"),
    ("jobTitle", "Job title", False, "select"),
    ("department", "Department", False, "select"),
    ("location", "Location", False, "select"),
    ("reportsTo", "Reports to", False, "text"),
    ("employmentType", "Employment type", False, "text"),
    ("employeeNumber", "Employee number", False, "text"),
    ("businessUnit", "Business unit", False, "text"),
    ("costCentre", "Cost centre", False, "text"),
)
_LOOKUPS = {"jobTitle": "/hris/jobtitles", "department": "/hris/departments", "location": "/hris/locations"}


class KekaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_slug: str = Field(default="", max_length=60, pattern=r"^[a-z0-9-]{0,60}$")
    environment: Literal["keka", "kekademo"] = "keka"


def keka_status(raw: Any) -> Literal["active", "exited", "pending", "unknown"]:
    value = str(raw or "").strip().lower()
    if value in ("0", "active", "working", "confirmed", "probation"):
        return "active"
    if value in ("1", "2", "relieved", "exited", "terminated", "resigned", "inactive", "separated"):
        return "exited"
    if value in ("pending", "notjoined", "not_joined", "preboarding"):
        return "pending"
    return "unknown"


def _date(raw: Any) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


class KekaHrms(LiveHrmsBase):
    provider_key = "keka"

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self._token: str | None = None
        self._token_expires: datetime | None = None

    @property
    def _cfg(self) -> KekaConfig:
        return self.ctx.config  # type: ignore[return-value]

    def _base(self) -> str:
        cfg = self._cfg
        if not cfg.company_slug:
            msg = "The Keka company is not configured"
            raise ConnectorAuthError(msg)
        return f"https://{cfg.company_slug}.{cfg.environment}.com/api/v1"

    async def _auth(self) -> dict[str, str]:
        now = self.ctx.now()
        if self._token and self._token_expires and now < self._token_expires:
            return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        secrets = self.ctx.secrets
        client_id, client_secret, api_key = secrets.get("client_id"), secrets.get("client_secret"), secrets.get("api_key")
        if not (client_id and client_secret and api_key):
            msg = "The Keka API credentials are missing"
            raise ConnectorAuthError(msg)
        resp = await self.ctx.http.post(
            TOKEN_URL,
            data={"grant_type": "kekaapi", "scope": "kekaapi", "client_id": client_id, "client_secret": client_secret, "api_key": api_key},
            headers={"Accept": "application/json"},
        )
        classify(resp, "token")
        body = resp.json()
        token = body.get("access_token") if isinstance(body, dict) else None
        if not token:
            msg = "Keka did not return an access token"
            raise ConnectorAuthError(msg)
        self._token = str(token)
        self._token_expires = now + timedelta(seconds=max(int(body.get("expires_in") or 3600) - 60, 60))
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

    async def _call(self, method: str, path: str, what: str, **kw: Any) -> Any:
        headers = await self._auth()
        resp = await self.ctx.http.request(method, f"{self._base()}{path}", headers=headers, **kw)
        classify(resp, what)
        return resp.json() if resp.content else {}

    async def field_schema(self) -> FieldSchemaDTO:
        options: dict[str, tuple[tuple[str, str], ...]] = {}
        for key, path in _LOOKUPS.items():
            body = await self._call("GET", path, f"{key} list")
            items = body.get("data") if isinstance(body, dict) else body
            opts = []
            for item in items or []:
                if isinstance(item, dict) and item.get("id"):
                    opts.append((str(item["id"]), str(item.get("name") or item.get("title") or item["id"])[:200]))
            options[key] = tuple(opts)
        return FieldSchemaDTO(
            fields=tuple(
                BoardFieldDTO(k, label, req, kind, options.get(k, ()))  # type: ignore[arg-type]
                for k, label, req, kind in _FIELDS
            )
        )

    async def push_hire(self, hire: HireDTO) -> HrmsResultDTO:
        body = await self._call("POST", "/hris/employees", "create employee", json=dict(hire.fields))
        data = body.get("data") if isinstance(body, dict) else None
        data = data if isinstance(data, dict) else (body if isinstance(body, dict) else {})
        ref = data.get("id")
        if not ref:
            return HrmsResultDTO(employee_ref=None, status="pending", message="Keka accepted the record; the employee id will arrive with the status pull")
        return HrmsResultDTO(employee_ref=str(ref), status="created", message=str(data.get("employeeNumber") or "") or None)

    async def push_change(self, change: ChangeDTO) -> HrmsResultDTO:
        await self._call("PUT", f"/hris/employees/{change.employee_ref}", "update employee", json=dict(change.fields))
        return HrmsResultDTO(employee_ref=change.employee_ref, status="updated", message=None)

    async def push_exit(self, employee_ref: str, last_day: date) -> HrmsResultDTO:
        headers = await self._auth()
        resp = await self.ctx.http.post(
            f"{self._base()}/hris/employees/{employee_ref}/exit", headers=headers, json={"lastWorkingDate": last_day.isoformat()}
        )
        if resp.status_code in (404, 405):
            raise ManualActionRequired("Record the exit in Keka", {"op": "push_exit", "employee_ref": employee_ref, "last_day": last_day.isoformat()})
        classify(resp, "employee exit")
        return HrmsResultDTO(employee_ref=employee_ref, status="updated", message=None)

    async def pull_status(self, since: datetime | None, cursor: str | None) -> tuple[Sequence[HrmsStatusDTO], str | None]:
        page = int(cursor) if cursor and cursor.isdigit() else 1
        params: dict[str, Any] = {"pageNumber": page, "pageSize": PAGE_SIZE}
        if since is not None:
            params["lastModified"] = since.isoformat()
        body = await self._call("GET", "/hris/employees", "list employees", params=params)
        items = body.get("data") if isinstance(body, dict) else body
        out: list[HrmsStatusDTO] = []
        for item in items or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            out.append(
                HrmsStatusDTO(
                    employee_ref=str(item["id"]),
                    employee_code=str(item.get("employeeNumber") or "")[:60] or None,
                    status=keka_status(item.get("employmentStatus")),
                    exit_date=_date(item.get("exitDate") or item.get("lastWorkingDate")),
                )
            )
        total_pages = int(body.get("totalPages") or page) if isinstance(body, dict) else page
        return out, (str(page + 1) if page < total_pages else None)
