# ruff: file-ignore[line-too-long]
"""Keka payroll adapter (Epic 71, spec A6; key ``keka_payroll``). ``push_export`` uploads the approved Payroll Export
as pay inputs through the payroll-inputs import endpoint where the Tenant's Keka plan exposes it
(``POST /payroll/payinputs/import``, multipart); a 404/405 (not on the plan) -> ``ManualActionRequired`` (download the
file and upload it in Keka) -- the export stays ``not_sent`` per P2 rules. Same credentials as the Keka HRMS twin
(separate install)."""

import uuid
from collections.abc import Mapping
from typing import Any

from app.core.integrations.errors import ManualActionRequired
from app.services.connectors.hrms.keka import KekaHrms
from app.services.connectors.hrms.live_base import classify

__all__ = ["KekaPayroll"]


class KekaPayroll(KekaHrms):
    category = "payroll"  # type: ignore[assignment]
    provider_key = "keka_payroll"

    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        if op == "health":
            await self._auth()
            return {"ok": True}
        raise NotImplementedError(op)

    async def push_export(self, export_id: uuid.UUID, file: bytes, media_type: str) -> Mapping[str, Any]:
        headers = await self._auth()
        resp = await self.ctx.http.post(
            f"{self._base()}/payroll/payinputs/import",
            headers=headers,
            files={"file": (f"payroll-{export_id}.{'csv' if 'csv' in media_type else 'xlsx'}", file, media_type)},
        )
        if resp.status_code in (404, 405):
            raise ManualActionRequired("Upload the payroll file in Keka", {"op": "push_export", "export_id": str(export_id)})
        classify(resp, "payroll inputs import")
        body = resp.json() if resp.content else {}
        ref = (body.get("data") or {}).get("id") if isinstance(body, dict) and isinstance(body.get("data"), dict) else None
        return {"ok": True, "external_ref": str(ref) if ref else f"keka-{export_id}"}

    async def get_status(self, external_ref: str) -> Any:
        return {"ok": True, "external_ref": external_ref}
