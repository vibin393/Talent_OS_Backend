# ruff: file-ignore[line-too-long]
"""Darwinbox payroll adapter (Epic 71, spec A6; key ``darwinbox_payroll``). Darwinbox payroll-input imports are
client-specific (spec Known gap 1): with ``payroll_import_path`` configured the approved export is posted there
(base64 file + ``api_key``); otherwise ``ManualActionRequired`` (download the file and upload it in Darwinbox)."""

import base64
import uuid
from collections.abc import Mapping
from typing import Any

from pydantic import ConfigDict, Field

from app.core.integrations.errors import ManualActionRequired
from app.services.connectors.hrms.darwinbox import DarwinboxConfig, DarwinboxHrms
from app.services.connectors.hrms.live_base import classify

__all__ = ["DarwinboxPayroll", "DarwinboxPayrollConfig"]


class DarwinboxPayrollConfig(DarwinboxConfig):
    model_config = ConfigDict(extra="forbid")

    payroll_import_path: str | None = Field(default=None, max_length=200, pattern=r"^/[A-Za-z0-9/_-]*$")


class DarwinboxPayroll(DarwinboxHrms):
    category = "payroll"  # type: ignore[assignment]
    provider_key = "darwinbox_payroll"

    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        if op == "health":
            self._headers()
            return {"ok": True}
        raise NotImplementedError(op)

    async def push_export(self, export_id: uuid.UUID, file: bytes, media_type: str) -> Mapping[str, Any]:
        path = getattr(self.ctx.config, "payroll_import_path", None)
        if not path:
            raise ManualActionRequired("Upload the payroll file in Darwinbox", {"op": "push_export", "export_id": str(export_id)})
        resp = await self.ctx.http.post(
            self._url(path),
            headers=self._headers(),
            json=self._body({"file_name": f"payroll-{export_id}", "media_type": media_type, "file": base64.b64encode(file).decode()}),
        )
        classify(resp, "payroll import")
        return {"ok": True, "external_ref": f"darwinbox-{export_id}"}

    async def get_status(self, external_ref: str) -> Any:
        return {"ok": True, "external_ref": external_ref}
