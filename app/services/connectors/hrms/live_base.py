# ruff: noqa: E501
"""Shared helpers of the real HRMS adapters (Epic 71, spec A3): error classification and the generic port surface.

Adapters see only ``core.integrations``, ``core.net_safety``, their DTOs and their own package. Every HTTP call goes
through ``AdapterContext.http`` (the Egress Guard limited to the manifest hosts + the tenant host). Field values,
tokens and provider bodies never appear in ``repr``, logs or exception messages (field NAMES may).
"""

from collections.abc import Mapping
from typing import Any, ClassVar

from app.core.integrations.context import AdapterContext
from app.core.integrations.errors import (
    ConnectorAuthError,
    ConnectorPermanentError,
    ConnectorRateLimited,
    ConnectorTransientError,
)
from app.core.net_safety import SafeResponse

__all__ = ["LiveHrmsBase", "classify", "field_errors"]


def field_errors(resp: SafeResponse) -> list[str]:
    """Field NAMES from a 400/422 validation body (``errors``: list of {field|name} or {field: [...]}) -- never values."""
    try:
        body = resp.json()
    except Exception:
        return []
    names: list[str] = []
    errs = body.get("errors") if isinstance(body, Mapping) else None
    if isinstance(errs, Mapping):
        names = [str(k) for k in errs]
    elif isinstance(errs, list):
        for e in errs:
            if isinstance(e, Mapping):
                name = e.get("field") or e.get("name") or e.get("key")
                if name:
                    names.append(str(name))
    return sorted({n[:100] for n in names})


def classify(resp: SafeResponse, what: str) -> None:
    """Raise the classified connector error for a non-2xx response (spec A3 Errors)."""
    code = resp.status_code
    if 200 <= code < 300:
        return
    if code in (401, 403):
        msg = f"The HR system rejected the credentials ({what})"
        raise ConnectorAuthError(msg)
    if code == 429:
        retry = resp.headers.get("retry-after", "30")
        raise ConnectorRateLimited(float(retry) if retry.strip().isdigit() else 30.0)
    if code >= 500:
        msg = f"HR system error {code} ({what})"
        raise ConnectorTransientError(msg)
    if code in (400, 422):
        names = field_errors(resp)
        suffix = f": {', '.join(names)}" if names else ""
        raise ConnectorPermanentError("hrms_validation", f"The HR system refused the record{suffix}")
    raise ConnectorPermanentError(f"hrms_{code}", f"The HR system refused the request ({what}, HTTP {code})")


class LiveHrmsBase:
    category: ClassVar[str] = "hrms"
    provider_key: ClassVar[str] = ""

    def __init__(self, ctx: AdapterContext) -> None:
        self.ctx = ctx
        self.key = type(self).provider_key

    def __repr__(self) -> str:
        return f"{type(self).__name__}(mode={self.ctx.mode})"

    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:  # noqa: ARG002
        if op == "health":
            await self.field_schema()  # type: ignore[attr-defined]
            return {"ok": True}
        raise NotImplementedError(op)

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:  # noqa: ARG002
        return False

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # noqa: ARG002
        return {}
